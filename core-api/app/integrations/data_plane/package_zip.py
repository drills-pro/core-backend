"""The zips the Ray jobs run (`ray-jobs/ray_jobs`, `ray-training/ray_core`, `ray-serving/ray_serving`), without S3 or Postgres:
how a package is built from the source tree, and what makes a zip a package or a task bundle (one set of size, hash and
archive checks, then each kind's own file rules)."""
import functools
import hashlib
import io
import zipfile
from http import HTTPStatus
from pathlib import Path

from app.core.enums import JobPackageName
from app.core.problem import Problem
from app.core.requirements import RequirementsError, parse_lock
from app.integrations.data_plane import layout

PACKAGE_DIR = Path(__file__).parents[4] / "ray-jobs" / "ray_jobs"  # /ray-jobs/ray_jobs in the image, <repo>/ray-jobs/ray_jobs in the checkout
TRAINING_PACKAGE_DIR = Path(__file__).parents[4] / "ray-training" / "ray_core"  # the train driver and the contract (the ray_training package), same layout
SERVING_PACKAGE_DIR = Path(__file__).parents[4] / "ray-serving" / "ray_serving"  # the shared Serve apps, same layout
ZIP_ROOT = "package"  # Ray unpacks a remote zip's single top-level directory in place of the archive, so the package sits one level down
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)  # the zip epoch: the same sources always give the same bytes, hence the same hash
PACKAGE_DIRS = {JobPackageName.RAY_JOBS: PACKAGE_DIR, JobPackageName.RAY_TRAINING: TRAINING_PACKAGE_DIR, JobPackageName.RAY_SERVING: SERVING_PACKAGE_DIR}
INVALID_PACKAGE = "invalid_package"
INVALID_REQUIREMENTS = "invalid_requirements"  # a requirements.lock or manifest `requirements` that is not exact pins
LOCK_FILE = "requirements.lock"  # inside ZIP_ROOT, next to the package directory: Ray still unpacks one top-level directory
MAX_PACKAGE_BYTES = 20 * 1024 * 1024  # an object larger than this is not a package of pure-Python sources


@functools.cache
def build(package_dir: Path = PACKAGE_DIR) -> tuple[bytes, str]:
    """(zip bytes, staging key). Listed as a job's py_module, Ray unpacks it and the package (`package/ray_jobs/`) is importable.
    A `requirements.lock` inside the package directory goes to `package/requirements.lock`, next to the package."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(p for p in package_dir.rglob("*.py") if "__pycache__" not in p.parts):
            info = zipfile.ZipInfo(f"{ZIP_ROOT}/{package_dir.name}/{path.relative_to(package_dir).as_posix()}", ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())
        lock = package_dir / LOCK_FILE  # the package's pinned requirements (planning/26), read back by validate_package
        if lock.is_file():
            info = zipfile.ZipInfo(f"{ZIP_ROOT}/{LOCK_FILE}", ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, lock.read_bytes())
    data = buf.getvalue()
    return data, package_key(package_dir, hashlib.sha256(data).hexdigest())


def package_key(package_dir: Path, sha256: str) -> str:
    return f"{layout.JOB_PACKAGE_ROOT}/{package_dir.parent.name}-{sha256}.zip"


def invalid(detail: str, code: str = INVALID_PACKAGE) -> Problem:
    return Problem(HTTPStatus.UNPROCESSABLE_ENTITY, code, detail)


def open_archive(data: bytes, sha256: str, noun: str) -> zipfile.ZipFile:
    """The zip in `data` once it is what its key promised: within the size cap, hashing to `sha256`, a readable archive.
    `noun` ("package", "bundle") words the refusals."""
    if len(data) > MAX_PACKAGE_BYTES:
        raise invalid(f"a {noun} is at most {MAX_PACKAGE_BYTES} bytes")
    if hashlib.sha256(data).hexdigest() != sha256:
        raise invalid("the object does not hash to the sha256 in its key")
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise invalid("the object is not a zip archive") from None


def validate_package(name: JobPackageName, data: bytes, sha256: str) -> list[str]:
    """What CI built for `name`: pure-Python sources under the package's own directory (and its requirements.lock), hashing
    to `sha256`. Returns the lock's pins; [] when it has none (the package runs on the baked image)."""
    zf = open_archive(data, sha256, "package")
    names = zf.namelist()
    root = f"{ZIP_ROOT}/{PACKAGE_DIRS[name].name}/"
    lock = f"{ZIP_ROOT}/{LOCK_FILE}"
    if not names or any(n != lock and (not n.startswith(root) or not n.endswith(".py") or ".." in n.split("/")) for n in names):
        raise invalid(f"every file must be a .py under {root} (or {lock})")
    if lock not in names:
        return []
    try:
        return parse_lock(zf.read(lock).decode())
    except (RequirementsError, UnicodeDecodeError) as e:
        raise invalid(f"{LOCK_FILE}: {e}", INVALID_REQUIREMENTS) from None
