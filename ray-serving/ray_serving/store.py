"""The staging bucket (S3, write only) and presigned HTTP reads, for the batch job. Same shape as ray_core.store (the packages
ship as separate zips, so they cannot share it). boto3 is imported lazily."""
import os
from pathlib import Path
from urllib.request import urlopen

from ray_serving.constants import HTTP_TIMEOUT_SECONDS

CHECKSUM_ENV = ("AWS_REQUEST_CHECKSUM_CALCULATION", "AWS_RESPONSE_CHECKSUM_VALIDATION")  # same as ray_core.store (drift-tested in core-api)
CHECKSUM_WHEN_REQUIRED = "when_required"
LOCAL_SCHEME = "file://"  # STAGING_ENDPOINT=file:///dir writes under /dir/<prefix>: local runs and tests, no S3 needed


class S3Store:
    """Staging bucket under one prefix."""

    def __init__(self, bucket: str, prefix: str, endpoint: str, access_key: str, secret_key: str):
        import boto3

        # boto3 >= 1.36 signs a checksum trailer into every PutObject unless told otherwise; the staging store (S3-compatible, not AWS)
        # answers with SignatureDoesNotMatch. core-api sets these in the job's env too; setdefault keeps an explicit value.
        for name in CHECKSUM_ENV:
            os.environ.setdefault(name, CHECKSUM_WHEN_REQUIRED)
        self.bucket, self.prefix = bucket, prefix
        self.client = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access_key, aws_secret_access_key=secret_key)

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self.prefix + key, Body=data)


class DirStore:
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


def http_get(url: str) -> bytes:
    with urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310 - presigned URL core-api put in the spec (file:// in tests)
        return resp.read()
