"""The staging bucket (S3, or a local directory) and plain HTTP reads. boto3 is imported when an S3Store is built."""
import base64
import os
from pathlib import Path

from ray_common.constants import CHECKSUM_ENV, CHECKSUM_WHEN_REQUIRED, HTTP_TIMEOUT_SECONDS, LOCAL_SCHEME


class S3Store:
    """Staging bucket under one prefix."""

    def __init__(self, bucket: str, prefix: str, endpoint: str, access_key: str, secret_key: str):
        import boto3

        # The env vars are read by every boto3 version; the older ones ignore them. setdefault keeps a value the job set.
        for name in CHECKSUM_ENV:
            os.environ.setdefault(name, CHECKSUM_WHEN_REQUIRED)
        self.bucket, self.prefix = bucket, prefix
        self.client = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access_key, aws_secret_access_key=secret_key)

    def size(self, key: str) -> int:
        return self.client.head_object(Bucket=self.bucket, Key=self.prefix + key)["ContentLength"]

    def download(self, key: str, path: Path) -> None:
        self.client.download_file(self.bucket, self.prefix + key, str(path))

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self.prefix + key, Body=data)


class DirStore:
    """The same `put`, into a directory: STAGING_ENDPOINT=file:///dir."""

    def __init__(self, root: str, prefix: str):
        self.root = Path(root) / prefix

    def put(self, key: str, data: bytes) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def store_from_env(bucket: str, prefix: str):
    """Staging credentials are env vars set by the submitter, never in the spec."""
    endpoint = os.environ["STAGING_ENDPOINT"]
    if endpoint.startswith(LOCAL_SCHEME):
        return DirStore(endpoint.removeprefix(LOCAL_SCHEME), prefix)
    return S3Store(bucket, prefix, endpoint, os.environ["STAGING_ACCESS_KEY"], os.environ["STAGING_SECRET_KEY"])


def data_url(data: bytes) -> str:
    """A `data:` URL http_get serves: how a smoke spec carries its fixture, so any node can read it."""
    return "data:application/octet-stream;base64," + base64.b64encode(data).decode()


class ResponseTooLarge(Exception):
    """http_get read more than `max_bytes`."""


def http_get(url: str, max_bytes: int | None = None) -> bytes:
    """GET a presigned, huggingface.co, `data:` or (in tests) file:// URL; with max_bytes, stop reading one byte past the cap."""
    from urllib.request import urlopen  # looked up per call: tests patch urllib.request.urlopen

    with urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310 - a URL core-api put in the spec, or this job built
        data = resp.read(None if max_bytes is None else max_bytes + 1)
    if max_bytes is not None and len(data) > max_bytes:
        raise ResponseTooLarge(f"a download is over {max_bytes} bytes")
    return data
