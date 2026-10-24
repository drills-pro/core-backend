"""lakeFS and staging-bucket operations: one repository per dataset, one commit per version.

This package is the only code that reads the `lakefs` and `staging` secrets or builds a lakeFS, S3 or
MLflow client (tests/architecture enforces it). Every public operation takes the caller's tenant and
resource ids, never names, and checks ownership first (access.py).
"""
import asyncio
import contextlib
import posixpath
from urllib.parse import urlparse

import boto3
import httpx
from botocore.client import Config
from botocore.exceptions import ClientError

from app.core import secret_files
from app.core.problem import Problem
from app.core.settings import settings
from app.integrations.data_plane import access, layout
from app.integrations.data_plane.report import ImportReport

_HTTP_TIMEOUT = 10
_POLL_SECONDS = 1.0
_HTTP_NOT_FOUND, _HTTP_CONFLICT = 404, 409
_S3_NO_SUCH_KEY = "NoSuchKey"
_HTTP_UNPROCESSABLE = 422
INVALID_STAGED_OUTPUT = "invalid_staged_output"
INVALID_PATH = "invalid_path"
IMPORT_ID_METADATA = "import_id"  # commit metadata key: lets a retried commit find the commit an earlier try made
_COMMIT_SCAN_AMOUNT = 100  # how many recent commits on main a retry searches for its import's commit
_OUT_FILES = frozenset({layout.INDEX_KEY, layout.DATA_KEY, layout.SCHEMA_KEY})


def _lakefs_api() -> httpx.AsyncClient:
    creds = secret_files.get(secret_files.LAKEFS)
    return httpx.AsyncClient(
        base_url=f"{creds['endpoint']}/api/v1", auth=(creds["access_key"], creds["secret_key"]), timeout=_HTTP_TIMEOUT
    )


def _s3(secret: str, endpoint: str | None = None):
    creds = secret_files.get(secret)
    return boto3.client(
        "s3",
        endpoint_url=endpoint or creds["endpoint"],
        aws_access_key_id=creds["access_key"],
        aws_secret_access_key=creds["secret_key"],
        config=Config(signature_version="s3v4"),
    )


async def health() -> str:
    """Proves the lakeFS and staging credentials and endpoints work right now, with fresh clients."""
    try:
        await asyncio.to_thread(_s3(secret_files.LAKEFS).list_buckets)
        await asyncio.to_thread(_s3(secret_files.STAGING).list_buckets)
        return layout.HEALTH_OK
    except Exception as e:  # noqa: BLE001 - health check, report any failure
        return f"{layout.HEALTH_ERROR}{e}"


def _checked_path(path: str) -> str:
    clean = posixpath.normpath(path)
    if not path or path.startswith("/") or clean.startswith("..") or clean != path:
        raise Problem(400, INVALID_PATH, "invalid object path")
    return path


def _namespace(tenant_id: str, dataset_id: str) -> str:
    return f"{tenant_id}/{dataset_id}/"


async def create_dataset_repo(tenant_id: str, dataset_id: str) -> None:
    """Create the dataset's repository; creating it twice is fine."""
    await access.dataset(tenant_id, dataset_id)
    body = {
        "name": layout.repo_name(dataset_id),
        "storage_namespace": f"s3://{settings.data_plane.lakefs_blockstore_bucket}/{_namespace(tenant_id, dataset_id)}",
        "default_branch": layout.MAIN_BRANCH,
    }
    async with _lakefs_api() as client:
        resp = await client.post("/repositories", json=body)
    if resp.status_code != _HTTP_CONFLICT:
        resp.raise_for_status()


async def presign_staging_put(tenant_id: str, dataset_id: str, import_id: str) -> str:
    """URL the browser PUTs the archive or CSV to; the key is fixed by the layout, not chosen by the caller."""
    await access.dataset_import(tenant_id, dataset_id, import_id)
    key = layout.staging_prefix(tenant_id, dataset_id, import_id) + layout.UPLOAD_KEY
    return _s3(secret_files.STAGING, settings.public.staging_endpoint).generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.data_plane.staging_bucket, "Key": key},
        ExpiresIn=settings.data_plane.presign_ttl_seconds,
    )


async def presign_get(tenant_id: str, dataset_id: str, version_id: str, path: str) -> str:
    """URL for one object of a committed version, read through the lakeFS S3 gateway at the version's commit."""
    commit = await access.version_commit(tenant_id, dataset_id, version_id)
    return _s3(secret_files.LAKEFS, settings.public.lakefs_endpoint).generate_presigned_url(
        "get_object",
        Params={"Bucket": layout.repo_name(dataset_id), "Key": f"{commit}/{_checked_path(path)}"},
        ExpiresIn=settings.data_plane.presign_ttl_seconds,
    )


def _parquet_head(secret: str, location: str, limit: int) -> list[dict]:
    """The first `limit` rows of a parquet object; only the footer and the first row group are fetched."""
    from pyarrow import fs  # noqa: PLC0415 - imported lazily: only the preview routes need it
    from pyarrow.parquet import ParquetFile  # noqa: PLC0415

    creds = secret_files.get(secret)
    endpoint = urlparse(creds["endpoint"])
    s3 = fs.S3FileSystem(
        access_key=creds["access_key"], secret_key=creds["secret_key"], endpoint_override=endpoint.netloc, scheme=endpoint.scheme
    )
    with s3.open_input_file(location) as f:
        batch = next(ParquetFile(f).iter_batches(batch_size=limit), None)
    return batch.to_pylist() if batch is not None else []


async def read_version_sample(tenant_id: str, dataset_id: str, version_id: str, key: str, limit: int) -> list[dict]:
    """First rows of `key` (index.parquet or data.parquet) in a committed version, read at its commit."""
    commit = await access.version_commit(tenant_id, dataset_id, version_id)
    location = f"{layout.repo_name(dataset_id)}/{commit}/{_checked_path(key)}"
    return await asyncio.to_thread(_parquet_head, secret_files.LAKEFS, location, limit)


async def read_staged_sample(tenant_id: str, dataset_id: str, import_id: str, key: str, limit: int) -> list[dict]:
    """First rows of `key` in the import's staged `out/` tree (the review preview)."""
    await access.dataset_import(tenant_id, dataset_id, import_id)
    location = f"{settings.data_plane.staging_bucket}/{layout.staging_prefix(tenant_id, dataset_id, import_id)}{layout.OUT_PREFIX}{_checked_path(key)}"
    return await asyncio.to_thread(_parquet_head, secret_files.STAGING, location, limit)


async def read_report(tenant_id: str, dataset_id: str, import_id: str) -> ImportReport | None:
    """The job's report.json, or None while the job has not written it yet."""
    await access.dataset_import(tenant_id, dataset_id, import_id)
    key = layout.staging_prefix(tenant_id, dataset_id, import_id) + layout.REPORT_KEY
    try:
        obj = await asyncio.to_thread(_s3(secret_files.STAGING).get_object, Bucket=settings.data_plane.staging_bucket, Key=key)
    except ClientError as e:
        if e.response["Error"]["Code"] == _S3_NO_SUCH_KEY:
            return None
        raise
    return ImportReport.model_validate_json(await asyncio.to_thread(obj["Body"].read))


def _list_staged(prefix: str) -> list[tuple[str, int]]:
    """(key relative to `prefix`, size in bytes) of everything under it in the staging bucket."""
    found = []
    for page in _s3(secret_files.STAGING).get_paginator("list_objects_v2").paginate(Bucket=settings.data_plane.staging_bucket, Prefix=prefix):
        found += [(o["Key"][len(prefix):], o["Size"]) for o in page.get("Contents", [])]
    return found


def _staged_violation(entries: list[tuple[str, int]]) -> str | None:
    """Why `out/` may not be imported, or None. The job is untrusted input: what it wrote is checked here, not believed."""
    limits = settings.dataset_limits
    top = {k for k, _ in entries if not k.startswith(layout.OBJECTS_PREFIX)}
    objects = [size for k, size in entries if k.startswith(layout.OBJECTS_PREFIX)]
    if any(k.startswith("/") or posixpath.normpath(k) != k for k, _ in entries):
        return "an output key is not a clean relative path"
    if top - _OUT_FILES:
        return f"unexpected output files {sorted(top - _OUT_FILES)}"
    if not top & {layout.INDEX_KEY, layout.DATA_KEY}:
        return "the output has no index or data file"
    if len(objects) > limits.max_files:
        return f"{len(objects)} objects, limit {limits.max_files}"
    if sum(objects) > limits.extracted_max_bytes:
        return f"{sum(objects)} object bytes, limit {limits.extracted_max_bytes}"
    if objects and max(objects) > limits.image_max_bytes:
        return f"an object is over {limits.image_max_bytes} bytes"
    return None


async def _existing_commit(client: httpx.AsyncClient, repo: str, import_id: str) -> str | None:
    resp = await client.get(f"/repositories/{repo}/refs/{layout.MAIN_BRANCH}/commits", params={"amount": _COMMIT_SCAN_AMOUNT})
    resp.raise_for_status()
    return next((c["id"] for c in resp.json()["results"] if (c.get("metadata") or {}).get(IMPORT_ID_METADATA) == import_id), None)


async def commit_staged(tenant_id: str, dataset_id: str, import_id: str, message: str) -> str:
    """Import the staged `out/` tree into the repo's main branch as one commit; returns the commit id.

    lakeFS reads the objects from the staging bucket itself, so no bytes pass through core-api. What is staged is
    checked first (limits, allowed file names). The commit carries the import id, so calling this again for the same
    import after a failure returns the commit already made instead of making a second one.
    """
    await access.dataset_import(tenant_id, dataset_id, import_id)
    prefix = layout.staging_prefix(tenant_id, dataset_id, import_id)
    repo = layout.repo_name(dataset_id)
    url = f"/repositories/{repo}/branches/{layout.MAIN_BRANCH}/import"
    async with _lakefs_api() as client:
        if (done := await _existing_commit(client, repo, import_id)) is not None:
            return done
        if violation := _staged_violation(await asyncio.to_thread(_list_staged, prefix + layout.OUT_PREFIX)):
            raise Problem(_HTTP_UNPROCESSABLE, INVALID_STAGED_OUTPUT, violation)
        source = f"s3://{settings.data_plane.staging_bucket}/{prefix}{layout.OUT_PREFIX}"
        body = {
            "paths": [{"type": "prefix", "path": source, "destination": ""}],
            "commit": {"message": message, "metadata": {IMPORT_ID_METADATA: import_id}},
        }
        started = await client.post(url, json=body)
        started.raise_for_status()
        job_id = started.json()["id"]
        try:
            async with asyncio.timeout(settings.data_plane.commit_timeout_seconds):
                while True:
                    status = await client.get(url, params={"id": job_id})
                    status.raise_for_status()
                    state = status.json()
                    if error := state.get("error"):
                        raise RuntimeError(f"lakeFS import failed: {error.get('message')}")
                    if state.get("completed"):
                        return state["commit"]["id"]
                    await asyncio.sleep(_POLL_SECONDS)
        except TimeoutError:
            with contextlib.suppress(httpx.HTTPError):  # best effort: a still-running import would block the retry
                await client.delete(url, params={"id": job_id})
            raise


def _delete_prefix(bucket: str, prefix: str, client) -> None:
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if keys:
            client.delete_objects(Bucket=bucket, Delete={"Objects": keys})


async def purge_staging(tenant_id: str, dataset_id: str, import_id: str) -> None:
    await access.dataset_import(tenant_id, dataset_id, import_id)
    prefix = layout.staging_prefix(tenant_id, dataset_id, import_id)
    await asyncio.to_thread(_delete_prefix, settings.data_plane.staging_bucket, prefix, _s3(secret_files.STAGING))


async def delete_dataset_repo(tenant_id: str, dataset_id: str) -> None:
    """Delete the repository and its storage namespace; already gone is fine, so the sweep can retry."""
    await access.dataset(tenant_id, dataset_id)
    async with _lakefs_api() as client:
        resp = await client.delete(f"/repositories/{layout.repo_name(dataset_id)}")
    if resp.status_code != _HTTP_NOT_FOUND:
        resp.raise_for_status()
    await asyncio.to_thread(
        _delete_prefix, settings.data_plane.lakefs_blockstore_bucket, _namespace(tenant_id, dataset_id), _s3(secret_files.STAGING)  # the staging credentials must cover the blockstore bucket (docs/runtime-contract.md)
    )

