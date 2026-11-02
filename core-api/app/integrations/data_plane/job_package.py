"""The job packages (`ray-jobs/ray_jobs`, `ray-training/ray_training`) as zips in the staging bucket, named by their content hash.

`publish` builds a zip from the source tree the image ships with, stages it once per version of the code and registers it
as a candidate in `job_packages`; an admin promotes it to stable (core/job_packages.py). Jobs run a registered package:
`presigned_url` hands the Ray job a GET for it and the job holds no extra credential. Outside prod, `resolve_stable`
publishes and promotes the bundled source itself, so a checkout runs without an admin step.
"""
import asyncio
import functools
import hashlib
import io
import zipfile
from pathlib import Path

from http import HTTPStatus

from botocore.exceptions import ClientError

from app.core import job_packages, secret_files
from app.core.db import get_pool
from app.core.enums import JobPackageName, PackageStatus
from app.core.job_packages import JobPackage
from app.core.problem import Problem
from app.core.settings import settings
from app.integrations.data_plane import layout, lakefs

PACKAGE_DIR = Path(__file__).parents[4] / "ray-jobs" / "ray_jobs"  # /ray-jobs/ray_jobs in the image, <repo>/ray-jobs/ray_jobs in the checkout
TRAINING_PACKAGE_DIR = Path(__file__).parents[4] / "ray-training" / "ray_training"  # the train driver and trainer, same layout
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)  # the zip epoch: the same sources always give the same bytes, hence the same hash
PACKAGE_DIRS = {JobPackageName.RAY_JOBS: PACKAGE_DIR, JobPackageName.RAY_TRAINING: TRAINING_PACKAGE_DIR}
SELF_PUBLISHER = "self-publish"  # the actor recorded when a non-prod core-api registers its own source
PROD_ENV = "prod"
NO_STABLE_PACKAGE = "no_stable_package"
_S3_NOT_FOUND = "404"
_uploaded: set[str] = set()


@functools.cache
def build(package_dir: Path = PACKAGE_DIR) -> tuple[bytes, str]:
    """(zip bytes, staging key). The archive root holds the package (`ray_jobs/`), so extracting it anywhere makes it importable."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(p for p in package_dir.rglob("*.py") if "__pycache__" not in p.parts):
            info = zipfile.ZipInfo(f"{package_dir.name}/{path.relative_to(package_dir).as_posix()}", ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())
    data = buf.getvalue()
    return data, f"{layout.JOB_PACKAGE_ROOT}/{package_dir.parent.name}-{hashlib.sha256(data).hexdigest()}.zip"


def _upload_once(data: bytes, key: str) -> None:
    if key in _uploaded:
        return
    client, bucket = lakefs.s3_client(secret_files.STAGING), settings.data_plane.staging_bucket
    try:
        client.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if e.response["Error"]["Code"] != _S3_NOT_FOUND:
            raise
        client.put_object(Bucket=bucket, Key=key, Body=data)
    _uploaded.add(key)


async def publish(name: JobPackageName, actor: str) -> JobPackage:
    """Stage the bundled source of `name` if this version is not in staging yet and register it (a candidate when new)."""
    data, key = build(PACKAGE_DIRS[name])
    await asyncio.to_thread(_upload_once, data, key)
    async with (await get_pool()).acquire() as conn:
        return await job_packages.register(conn, name, hashlib.sha256(data).hexdigest(), key, actor)


async def resolve_stable(name: JobPackageName) -> JobPackage:
    """The package a new job runs: the newest stable one. Outside prod the bundled source is published and promoted first."""
    if settings.env != PROD_ENV:
        package = await publish(name, SELF_PUBLISHER)
        if package.status == PackageStatus.CANDIDATE:
            async with (await get_pool()).acquire() as conn:
                await job_packages.set_status(conn, name, package.sha256, PackageStatus.STABLE, SELF_PUBLISHER)
    async with (await get_pool()).acquire() as conn:
        stable = await job_packages.latest_stable(conn, name)
    if stable is None:
        raise Problem(HTTPStatus.SERVICE_UNAVAILABLE, NO_STABLE_PACKAGE, f"no stable {name} package is published")
    return stable


async def presigned_url(name: JobPackageName, sha256: str) -> str:
    """A GET the Ray job can reach (signed for the endpoint of the `ingest` credential, which is the address Ray uses)
    for the registered package, whatever its status: a run keeps the package it was created with."""
    async with (await get_pool()).acquire() as conn:
        package = await job_packages.get(conn, name, sha256)
    if package is None:
        raise LookupError(f"{name} package {sha256} is not registered")
    return lakefs.s3_client(secret_files.INGEST).generate_presigned_url(
        "get_object", Params={"Bucket": settings.data_plane.staging_bucket, "Key": package.s3_key},
        ExpiresIn=settings.data_plane.presign_ttl_seconds)
