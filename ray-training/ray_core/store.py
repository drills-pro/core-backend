"""The staging bucket (S3, write only) and presigned HTTP reads. boto3 is imported lazily."""
import base64
import os
from pathlib import Path
from urllib.request import urlopen

from ray_core.constants import HTTP_TIMEOUT_SECONDS, TRAIN_STATE_DIR

CHECKSUM_ENV = ("AWS_REQUEST_CHECKSUM_CALCULATION", "AWS_RESPONSE_CHECKSUM_VALIDATION")
CHECKSUM_WHEN_REQUIRED = "when_required"


class S3Store:
    """Staging bucket under one prefix."""

    def __init__(self, bucket: str, prefix: str, endpoint: str, access_key: str, secret_key: str):
        import boto3

        # boto3 >= 1.36 signs a checksum trailer into every PutObject unless told otherwise; the staging store (an S3-compatible
        # endpoint, not AWS) answers that with SignatureDoesNotMatch. The env vars are read by every boto3 version, the older
        # ones ignore them (the Config options of the same name do not exist there).
        for name in CHECKSUM_ENV:
            os.environ.setdefault(name, CHECKSUM_WHEN_REQUIRED)
        self.bucket, self.prefix = bucket, prefix
        self.client = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access_key, aws_secret_access_key=secret_key)

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self.prefix + key, Body=data)


def data_url(data: bytes) -> str:
    """A `data:` URL http_get serves: how a smoke spec carries its fixture, so any node can read it."""
    return "data:application/octet-stream;base64," + base64.b64encode(data).decode()


def http_get(url: str) -> bytes:
    with urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310 - presigned URL core-api put in the spec
        return resp.read()


LOCAL_SCHEME = "file://"  # STAGING_ENDPOINT=file:///dir writes under /dir/<prefix>: local runs and CI, no S3 needed


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


def train_state_storage(bucket: str, job_id: str):
    """(pyarrow filesystem, path) where Ray Train keeps a job's checkpoints: under a top-level train-state/<job_id>/ prefix, so a
    bucket lifecycle rule can expire them without touching a run's manifest and outputs under its run prefix."""
    from urllib.parse import urlsplit

    from pyarrow import fs

    endpoint = os.environ["STAGING_ENDPOINT"]
    if endpoint.startswith(LOCAL_SCHEME):
        return fs.LocalFileSystem(), f"{endpoint.removeprefix(LOCAL_SCHEME)}/{TRAIN_STATE_DIR}/{job_id}"
    url = urlsplit(endpoint)
    s3 = fs.S3FileSystem(
        access_key=os.environ["STAGING_ACCESS_KEY"], secret_key=os.environ["STAGING_SECRET_KEY"],
        endpoint_override=url.netloc, scheme=url.scheme)
    return s3, f"{bucket}/{TRAIN_STATE_DIR}/{job_id}"
