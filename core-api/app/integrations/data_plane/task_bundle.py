"""Task bundles (planning/25) in the staging bucket: `jobs/<slug>-<sha256>.zip`, a package of `.py` sources plus
manifest.json under the root Ray unpacks. Register reads the object back and validates it (size, hash, file types, the
manifest, its entry points by `ast`) before the candidate row exists; nothing in the zip is imported here.
"""
import asyncio
import hashlib
import io
import json
import re
import zipfile
from http import HTTPStatus

from pydantic import ValidationError

from app.core import job_packages
from app.core.bundle_manifest import CORE_CONTRACT, MANIFEST_FILE, BundleManifest, missing_entry_points
from app.core.db import get_pool
from app.core.job_packages import JobPackage, task_package_name
from app.core.problem import Problem
from app.integrations.data_plane import layout
from app.integrations.data_plane.job_package import INVALID_PACKAGE, MAX_PACKAGE_BYTES, ZIP_ROOT, _read_staged

INVALID_MANIFEST = "invalid_manifest"
SOURCE_SUFFIX = ".py"
BUNDLE_KEY = re.compile(rf"{re.escape(layout.JOB_PACKAGE_ROOT)}/(?P<slug>[a-z][a-z0-9-]*)-(?P<sha256>[0-9a-f]{{64}})\.zip")


def _invalid(detail: str, code: str = INVALID_PACKAGE) -> Problem:
    return Problem(HTTPStatus.UNPROCESSABLE_ENTITY, code, detail)


def check_bundle(data: bytes, sha256: str) -> BundleManifest:
    """The bundle's manifest, once the zip proves to be what CI built: `sha256`, only `.py` sources and manifest.json
    under ZIP_ROOT, a valid manifest for this core's contract whose entry points the sources define."""
    if len(data) > MAX_PACKAGE_BYTES:
        raise _invalid(f"a bundle is at most {MAX_PACKAGE_BYTES} bytes")
    if hashlib.sha256(data).hexdigest() != sha256:
        raise _invalid("the object does not hash to the sha256 in its key")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise _invalid("the object is not a zip archive") from None
    root, manifest_path = f"{ZIP_ROOT}/", f"{ZIP_ROOT}/{MANIFEST_FILE}"
    names = [n for n in zf.namelist() if not n.endswith("/")]
    if any(not n.startswith(root) or ".." in n.split("/") or not (n.endswith(SOURCE_SUFFIX) or n == manifest_path) for n in names):
        raise _invalid(f"every file must be a {SOURCE_SUFFIX} source or {MANIFEST_FILE} under {root}")
    if manifest_path not in names:
        raise _invalid(f"the bundle has no {manifest_path}", INVALID_MANIFEST)
    try:
        manifest = BundleManifest.model_validate(json.loads(zf.read(manifest_path)))
    except (ValueError, ValidationError) as e:  # JSONDecodeError is a ValueError
        raise _invalid(f"{MANIFEST_FILE}: {e}", INVALID_MANIFEST) from None
    if manifest.contract != CORE_CONTRACT:
        raise _invalid(f"the bundle targets contract {manifest.contract}, this core serves {CORE_CONTRACT}", INVALID_MANIFEST)
    sources = {n.removeprefix(root): zf.read(n) for n in names if n.endswith(SOURCE_SUFFIX)}
    if missing := missing_entry_points(manifest, sources):
        raise _invalid(f"not defined in the bundle: {', '.join(missing)}", INVALID_MANIFEST)
    return manifest


async def register_staged(s3_key: str, actor: str) -> JobPackage:
    """Register the bundle CI put in staging as a candidate of task_<slug>; the key's slug must be the manifest's."""
    match = BUNDLE_KEY.fullmatch(s3_key)
    if match is None:
        raise _invalid(f"expected a key like {layout.JOB_PACKAGE_ROOT}/<slug>-<sha256>.zip")
    slug, sha256 = match["slug"], match["sha256"]
    manifest = check_bundle(await asyncio.to_thread(_read_staged, s3_key), sha256)
    if manifest.slug != slug:
        raise _invalid(f"the key names {slug}, the manifest {manifest.slug}", INVALID_MANIFEST)
    async with (await get_pool()).acquire() as conn:
        return await job_packages.register_bundle(
            conn, task_package_name(slug), sha256, s3_key, manifest.model_dump(mode="json", by_alias=True), actor)
