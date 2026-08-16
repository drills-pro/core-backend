"""Real data versioning for datasets, on top of the LakeFS instance
already running in this stack (docker-compose.yml) but not previously
wired to anything. One shared repo ("platform-datasets"), one branch per
platform dataset (branch name = dataset UUID) — each branch's commit
history *is* that dataset's version history. Object bytes are addressed
through LakeFS's own S3 gateway (same wire protocol as MinIO, just a
different endpoint/bucket), so the function shapes mirror
app/object_storage.py deliberately — datasets.py swaps between them with
a dataset_id added, not a rewrite.
"""
import os

import boto3
import httpx
from botocore.client import Config

from app import vault

REPO = "platform-datasets"
# Same host-vs-container split as object_storage.py's MINIO_PUBLIC_ENDPOINT
# — presigned URLs are handed to the browser, which needs the published
# host address, not the internal docker-network name.
PUBLIC_ENDPOINT = os.environ.get("LAKEFS_PUBLIC_ENDPOINT", "http://localhost:8000")

_LAKEFS_API_BASE = None
_client = None
_presign_client = None
_ensured_branches: set[str] = set()


def _creds() -> dict:
    return vault.get_kv("platform/lakefs")


def _api_client() -> httpx.Client:
    global _LAKEFS_API_BASE
    creds = _creds()
    if _LAKEFS_API_BASE is None:
        _LAKEFS_API_BASE = f"{creds['endpoint']}/api/v1"
    return httpx.Client(base_url=_LAKEFS_API_BASE, auth=(creds["access_key"], creds["secret_key"]), timeout=10)


def _get_client():
    global _client
    if _client is None:
        creds = _creds()
        _client = boto3.client(
            "s3",
            endpoint_url=creds["endpoint"],
            aws_access_key_id=creds["access_key"],
            aws_secret_access_key=creds["secret_key"],
            config=Config(signature_version="s3v4"),
        )
    return _client


def _get_presign_client():
    global _presign_client
    if _presign_client is None:
        creds = _creds()
        _presign_client = boto3.client(
            "s3",
            endpoint_url=PUBLIC_ENDPOINT,
            aws_access_key_id=creds["access_key"],
            aws_secret_access_key=creds["secret_key"],
            config=Config(signature_version="s3v4"),
        )
    return _presign_client


def ensure_branch(dataset_id: str) -> None:
    """Idempotent: create this dataset's branch off `main` if it doesn't
    exist yet. Cached in-process — branch creation is one-time setup per
    dataset, not worth a round-trip on every request."""
    if dataset_id in _ensured_branches:
        return
    with _api_client() as client:
        resp = client.get(f"/repositories/{REPO}/branches/{dataset_id}")
        if resp.status_code == 404:
            create = client.post(f"/repositories/{REPO}/branches", json={"name": dataset_id, "source": "main"})
            create.raise_for_status()
    _ensured_branches.add(dataset_id)


def commit(dataset_id: str, message: str) -> dict:
    """Snapshot the dataset branch's current state as a real LakeFS
    commit — the object-store equivalent of `git commit`."""
    with _api_client() as client:
        resp = client.post(f"/repositories/{REPO}/branches/{dataset_id}/commits", json={"message": message})
        resp.raise_for_status()
        return resp.json()


def list_versions(dataset_id: str) -> list[dict]:
    """Real commit log for this dataset's branch, newest-first."""
    with _api_client() as client:
        resp = client.get(f"/repositories/{REPO}/refs/{dataset_id}/commits")
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return resp.json().get("results", [])


def has_uncommitted_changes(dataset_id: str) -> bool:
    with _api_client() as client:
        resp = client.get(f"/repositories/{REPO}/branches/{dataset_id}/diff")
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return len(resp.json().get("results", [])) > 0


def presigned_put_url(dataset_id: str, key: str, expires_in: int = 3600, ref: str = "") -> str:
    ensure_branch(dataset_id)
    full_key = f"{ref or dataset_id}/{key}"
    return _get_presign_client().generate_presigned_url(
        "put_object", Params={"Bucket": REPO, "Key": full_key}, ExpiresIn=expires_in
    )


def presigned_get_url(dataset_id: str, key: str, expires_in: int = 3600, ref: str = "") -> str:
    full_key = f"{ref or dataset_id}/{key}"
    return _get_presign_client().generate_presigned_url(
        "get_object", Params={"Bucket": REPO, "Key": full_key}, ExpiresIn=expires_in
    )


def list_files(dataset_id: str, prefix: str = "", ref: str = "") -> list[str]:
    """Returns keys relative to the dataset (branch prefix stripped),
    same shape as object_storage.list_files did with s3_prefix."""
    full_prefix = f"{ref or dataset_id}/{prefix}"
    resp = _get_client().list_objects_v2(Bucket=REPO, Prefix=full_prefix)
    branch_prefix = f"{ref or dataset_id}/"
    return [obj["Key"].removeprefix(branch_prefix) for obj in resp.get("Contents", [])]


def download(dataset_id: str, key: str, ref: str = "") -> bytes:
    full_key = f"{ref or dataset_id}/{key}"
    return _get_client().get_object(Bucket=REPO, Key=full_key)["Body"].read()


def upload(dataset_id: str, key: str, data: bytes) -> None:
    ensure_branch(dataset_id)
    _get_client().put_object(Bucket=REPO, Key=f"{dataset_id}/{key}", Body=data)


def copy(dataset_id: str, src_key: str, dst_key: str) -> None:
    _get_client().copy_object(
        Bucket=REPO, CopySource={"Bucket": REPO, "Key": f"{dataset_id}/{src_key}"}, Key=f"{dataset_id}/{dst_key}"
    )


def delete(dataset_id: str, key: str) -> None:
    _get_client().delete_object(Bucket=REPO, Key=f"{dataset_id}/{key}")
