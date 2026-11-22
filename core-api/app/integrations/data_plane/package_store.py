"""The staging bucket as the job packages use it: put a zip once, read an object back (a missing one is not an error)."""
from botocore.exceptions import ClientError

from app.core import secret_files
from app.core.settings import settings
from app.integrations.data_plane import lakefs, package_zip

_NOT_FOUND_CODES = ("404", "NoSuchKey")
_uploaded: set[str] = set()


def _missing(error: ClientError) -> bool:
    return error.response["Error"]["Code"] in _NOT_FOUND_CODES


def upload_once(data: bytes, key: str) -> None:
    """Put `data` at `key` unless the bucket (or an earlier call of this process) already has it."""
    if key in _uploaded:
        return
    client, bucket = lakefs.s3_client(secret_files.STAGING), settings.data_plane.staging_bucket
    try:
        client.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if not _missing(e):
            raise
        client.put_object(Bucket=bucket, Key=key, Body=data)
    _uploaded.add(key)


def read(key: str, max_bytes: int | None = None) -> bytes | None:
    """The object at `key`, None when there is none. One over `max_bytes` is a 422 and is never downloaded."""
    client, bucket = lakefs.s3_client(secret_files.STAGING), settings.data_plane.staging_bucket
    try:
        if max_bytes is not None and client.head_object(Bucket=bucket, Key=key)["ContentLength"] > max_bytes:
            raise package_zip.invalid(f"a package is at most {max_bytes} bytes")
        return client.get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as e:
        if not _missing(e):
            raise
        return None


def read_staged(key: str) -> bytes:
    """The package or bundle zip CI staged at `key`; a missing or oversized one is a 422, not a crash."""
    data = read(key, package_zip.MAX_PACKAGE_BYTES)
    if data is None:
        raise package_zip.invalid(f"{key} is not in the staging bucket")
    return data
