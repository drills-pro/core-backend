"""Build a job package zip the way core-api does (app/integrations/data_plane/job_package.py build()), for CI to upload.

    python3 scripts/build-job-package.py ray_jobs|ray_training|ray_serving <out dir>

Writes <out dir>/<name>.zip and prints its sha256. The same sources always give the same bytes; a test in core-api
(tests/integrations/data_plane/test_job_package.py) fails if this and build() ever disagree.
"""
import hashlib
import io
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).parents[1]
DIRS = {"ray_jobs": ROOT / "ray-jobs" / "ray_jobs", "ray_training": ROOT / "ray-training" / "ray_core",  # the ray_training job package holds ray_core
        "ray_serving": ROOT / "ray-serving" / "ray_serving"}
ZIP_ROOT = "package"  # Ray unpacks a remote zip's single top-level directory in place of the archive
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def build(package_dir: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(p for p in package_dir.rglob("*.py") if "__pycache__" not in p.parts):
            info = zipfile.ZipInfo(f"{ZIP_ROOT}/{package_dir.name}/{path.relative_to(package_dir).as_posix()}", ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())
    return buf.getvalue()


if __name__ == "__main__":
    name, out = sys.argv[1], Path(sys.argv[2])
    data = build(DIRS[name])
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.zip").write_bytes(data)
    print(hashlib.sha256(data).hexdigest())
