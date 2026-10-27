"""The ray_jobs package (top-level `ray-jobs/`) as a zip in the staging bucket, named by its content hash.

core-api builds the zip from the source tree it ships with (the image carries `ray-jobs/ray_jobs`), uploads it once
per version of the code and hands the Ray job a presigned GET. The job holds no extra credential for it. Baking the
package into the Ray image instead keeps the same layout: `python -m ray_jobs.entrypoints.<name>` with no download.
"""
import asyncio
import functools
import hashlib
import io
import zipfile
from pathlib import Path

from botocore.exceptions import ClientError

from app.core import secret_files
from app.core.settings import settings
from app.integrations.data_plane import layout, lakefs

PACKAGE_DIR = Path(__file__).parents[4] / "ray-jobs" / "ray_jobs"  # /ray-jobs/ray_jobs in the image, <repo>/ray-jobs/ray_jobs in the checkout
TRAINING_PACKAGE_DIR = Path(__file__).parents[4] / "ray-training" / "ray_training"  # the train driver and trainer, same layout
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)  # the zip epoch: the same sources always give the same bytes, hence the same hash
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


async def presigned_url(package_dir: Path = PACKAGE_DIR) -> str:
    """Upload the package if this version is not in staging yet; returns a GET the Ray job can reach (signed for the
    endpoint of the `ingest` credential, which is the address Ray uses)."""
    data, key = build(package_dir)
    await asyncio.to_thread(_upload_once, data, key)
    return lakefs.s3_client(secret_files.INGEST).generate_presigned_url(
        "get_object", Params={"Bucket": settings.data_plane.staging_bucket, "Key": key},
        ExpiresIn=settings.data_plane.presign_ttl_seconds)
