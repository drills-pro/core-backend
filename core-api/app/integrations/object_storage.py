"""Real S3/MinIO object storage for customer dataset uploads (req/04:
presigned PUT, client uploads directly — the backend never proxies file
bytes; req/01: per-tenant key prefix)."""
import boto3
from botocore.client import Config

from app.core import config, vault

BUCKET = "datasets"
# FR4 (07-identity-oauth-linking.md): avatars aren't versioned dataset
# data, so they get their own bucket rather than living under BUCKET.
PLATFORM_ASSETS_BUCKET = "platform-assets"
# Presigned URLs are consumed by external clients (browser, curl from the
# host) — they need MinIO's host-published address, not the internal
# docker-network name core-api itself uses for server-side calls.
PUBLIC_ENDPOINT = config.MINIO_PUBLIC_ENDPOINT

_client = None
_presign_client = None


def _get_client():
    global _client
    if _client is None:
        creds = vault.get_kv("platform/minio")
        _client = boto3.client(
            "s3",
            endpoint_url="http://minio:9000",
            aws_access_key_id=creds["access_key"],
            aws_secret_access_key=creds["secret_key"],
            config=Config(signature_version="s3v4"),
        )
        existing = [b["Name"] for b in _client.list_buckets()["Buckets"]]
        for bucket in (BUCKET, PLATFORM_ASSETS_BUCKET):
            if bucket not in existing:
                _client.create_bucket(Bucket=bucket)
    return _client


def _get_presign_client():
    global _presign_client
    if _presign_client is None:
        creds = vault.get_kv("platform/minio")
        _presign_client = boto3.client(
            "s3",
            endpoint_url=PUBLIC_ENDPOINT,
            aws_access_key_id=creds["access_key"],
            aws_secret_access_key=creds["secret_key"],
            config=Config(signature_version="s3v4"),
        )
    return _presign_client


def presigned_put_url(key: str, expires_in: int = 3600, bucket: str = BUCKET) -> str:
    _get_client()  # ensures the bucket exists
    return _get_presign_client().generate_presigned_url(
        "put_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_in
    )


def list_files(prefix: str) -> list[str]:
    resp = _get_client().list_objects_v2(Bucket=BUCKET, Prefix=prefix)
    return [obj["Key"] for obj in resp.get("Contents", [])]


def download(key: str) -> bytes:
    return _get_client().get_object(Bucket=BUCKET, Key=key)["Body"].read()


def upload(key: str, data: bytes) -> None:
    _get_client().put_object(Bucket=BUCKET, Key=key, Body=data)


def presigned_get_url(key: str, expires_in: int = 3600) -> str:
    _get_client()
    return _get_presign_client().generate_presigned_url(
        "get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=expires_in
    )


def copy(src_key: str, dst_key: str) -> None:
    _get_client().copy_object(Bucket=BUCKET, CopySource={"Bucket": BUCKET, "Key": src_key}, Key=dst_key)


def delete(key: str) -> None:
    _get_client().delete_object(Bucket=BUCKET, Key=key)
