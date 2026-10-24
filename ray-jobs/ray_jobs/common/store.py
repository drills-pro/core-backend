"""The staging bucket (S3) and plain HTTP reads. boto3 is imported lazily."""
from pathlib import Path
from typing import Callable

from ray_jobs.common.constants import HTTP_TIMEOUT_SECONDS, UPLOAD_KEY
from ray_jobs.common.enums import Code
from ray_jobs.common.limits import LimitExceeded

Sink = Callable[[str, bytes], None]


class S3Store:
    """Staging bucket under one prefix. boto3 imported lazily."""

    def __init__(self, bucket: str, prefix: str, endpoint: str, access_key: str, secret_key: str):
        import boto3

        self.bucket, self.prefix = bucket, prefix
        self.client = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access_key, aws_secret_access_key=secret_key)

    def size(self, key: str) -> int:
        return self.client.head_object(Bucket=self.bucket, Key=self.prefix + key)["ContentLength"]

    def download(self, key: str, path: Path) -> None:
        self.client.download_file(self.bucket, self.prefix + key, str(path))

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self.prefix + key, Body=data)


def http_get(url: str, max_bytes: int | None = None) -> bytes:
    """GET a presigned or huggingface.co URL; with max_bytes, stop reading one byte past the cap."""
    from urllib.request import urlopen

    with urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310 - presigned or huggingface.co URL built by this job
        data = resp.read(None if max_bytes is None else max_bytes + 1)
    if max_bytes is not None and len(data) > max_bytes:
        raise LimitExceeded(Code.HF_TOO_LARGE, f"a download is over {max_bytes} bytes")
    return data


def fetch_upload(store, upload_max_bytes: int, tmp: Path) -> Path:
    """Download the uploaded archive or CSV; an oversized one is refused before it fills the disk."""
    if (uploaded := store.size(UPLOAD_KEY)) > upload_max_bytes:
        raise LimitExceeded(Code.ARCHIVE_TOO_LARGE, f"upload is {uploaded} bytes, limit {upload_max_bytes}")
    source = tmp / "source"
    store.download(UPLOAD_KEY, source)
    return source
