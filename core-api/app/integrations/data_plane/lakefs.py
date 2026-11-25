"""lakeFS and staging-bucket operations: one repository per dataset, one commit per version.

This package is the only code that reads the `lakefs` and `staging` secrets or builds a lakeFS, S3 or
MLflow client (tests/architecture enforces it). Every public operation takes the caller's tenant and
resource ids, never names, and checks ownership first (access.py).
"""
import asyncio
import io
import logging
import posixpath
from urllib.parse import urlparse

import boto3
import httpx
from botocore.client import Config
from botocore.exceptions import ClientError

from app.core import secret_files
from app.core.enums import BlockstoreScheme
from app.core.problem import Problem
from app.core.settings import DatasetLimits, settings
from app.integrations.data_plane import access, layout
from app.integrations.data_plane.report import ImportReport

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 10
_PARQUET_READ_BUFFER = 1 << 20  # bytes fetched per ranged GET while reading a preview
_HTTP_NOT_FOUND, _HTTP_CONFLICT = 404, 409
_S3_NO_SUCH_KEY = "NoSuchKey"
_HTTP_UNPROCESSABLE = 422
LINK_CONCURRENCY = 16  # staged objects copied and linked at a time
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
        # botocore >= 1.36 adds CRC32 trailer checksums to every PutObject; GCS's S3 interop API rejects them
        # (SignatureDoesNotMatch). Only send them where an operation requires one.
        config=Config(signature_version="s3v4", request_checksum_calculation="when_required",
                      response_checksum_validation="when_required"),
    )


def s3_client(secret: str):
    """A client for the named credential, for the other data_plane modules."""
    return _s3(secret)


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
        "storage_namespace": f"{settings.data_plane.lakefs_blockstore_scheme}://{settings.data_plane.lakefs_blockstore_bucket}/{_namespace(tenant_id, dataset_id)}",
        "default_branch": layout.MAIN_BRANCH,
    }
    async with _lakefs_api() as client:
        resp = await client.post("/repositories", json=body)
    if resp.status_code != _HTTP_CONFLICT:
        if resp.is_error:  # raise_for_status drops lakeFS's reason, which says what it rejected
            logger.error("lakeFS refused repository %s (%s): %s", body["name"], resp.status_code, resp.text)
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


async def _physical_object(repo: str, commit: str, path: str) -> tuple[str, str]:
    """Bucket and key lakeFS keeps `path` under at `commit`."""
    async with _lakefs_api() as client:
        resp = await client.get(f"/repositories/{repo}/refs/{commit}/objects/stat", params={"path": path})
    resp.raise_for_status()
    address = urlparse(resp.json()["physical_address"])
    return address.netloc, address.path.lstrip("/")


async def _presign_version(
    tenant_id: str, dataset_id: str, version_id: str, paths: list[str], ttl_seconds: int, *, public: bool
) -> dict[str, str]:
    """Presigned reads of objects of one committed version, as {path: url}, after one ownership check: through the
    lakeFS S3 gateway at the version's commit, or, when lakeFS keeps its data on GCS and has no public gateway,
    straight from the bucket it stores each object in. `public` signs for the browser-facing endpoints, otherwise
    for the in-network ones a job reaches."""
    commit = await access.version_commit(tenant_id, dataset_id, version_id)
    repo, paths = layout.repo_name(dataset_id), [_checked_path(p) for p in paths]
    on_gcs = settings.data_plane.lakefs_blockstore_scheme == BlockstoreScheme.GS
    if on_gcs:  # the staging credentials must cover the bucket lakeFS writes to (docs/runtime-contract.md)
        secret, endpoint = secret_files.STAGING, settings.public.staging_endpoint
    else:
        secret, endpoint = secret_files.LAKEFS, settings.public.lakefs_endpoint
    client = _s3(secret, endpoint if public else None)
    gate = asyncio.Semaphore(LINK_CONCURRENCY)

    async def one(path: str) -> tuple[str, str]:
        async with gate:
            bucket, key = await _physical_object(repo, commit, path) if on_gcs else (repo, f"{commit}/{path}")
        url = client.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=ttl_seconds)
        return path, url

    return dict(await asyncio.gather(*(one(p) for p in paths)))


async def presign_get(tenant_id: str, dataset_id: str, version_id: str, path: str) -> str:
    """Browser URL for one object of a committed version."""
    urls = await presign_gets(tenant_id, dataset_id, version_id, [path])
    return urls[path]


async def presign_gets(tenant_id: str, dataset_id: str, version_id: str, paths: list[str]) -> dict[str, str]:
    """Browser URLs for many objects of one committed version (a preview page), as {path: url}."""
    return await _presign_version(
        tenant_id, dataset_id, version_id, paths, settings.data_plane.presign_ttl_seconds, public=True
    )


async def presign_version_objects(tenant_id: str, dataset_id: str, version_id: str, paths: list[str], ttl_seconds: int) -> dict[str, str]:
    """Presigned reads of many objects of one version, as {path: url}, for a job that reaches the stores at their
    in-network addresses (the credentials' own endpoints, not the browser-facing ones of presign_gets)."""
    return await _presign_version(tenant_id, dataset_id, version_id, paths, ttl_seconds, public=False)


class _S3Object(io.RawIOBase):
    """Seekable read-only view of one object over ranged GETs, so pyarrow fetches only the footer and the first row group."""

    def __init__(self, client, bucket: str, key: str):
        self._client, self._bucket, self._key = client, bucket, key
        self._size = client.head_object(Bucket=bucket, Key=key)["ContentLength"]
        self._pos = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self._pos = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._size}[whence] + offset
        return self._pos

    def readinto(self, buf) -> int:
        if self._pos >= self._size or not len(buf):
            return 0
        end = min(self._pos + len(buf), self._size) - 1
        data = self._client.get_object(Bucket=self._bucket, Key=self._key, Range=f"bytes={self._pos}-{end}")["Body"].read()
        buf[: len(data)] = data
        self._pos += len(data)
        return len(data)


def _parquet_head(secret: str, location: str, limit: int) -> list[dict]:
    """The first `limit` rows of the parquet object at `bucket/key`; only the footer and the first row group are fetched."""
    from pyarrow.parquet import ParquetFile  # noqa: PLC0415 - imported lazily: only the preview routes need it

    bucket, _, key = location.partition("/")
    # boto3 rather than pyarrow's own S3 client: _s3 carries the GCS-compatible settings and path rules every other read uses
    with io.BufferedReader(_S3Object(_s3(secret), bucket, key), _PARQUET_READ_BUFFER) as f:
        batch = next(ParquetFile(f).iter_batches(batch_size=limit), None)
    return batch.to_pylist() if batch is not None else []


async def read_version_sample(tenant_id: str, dataset_id: str, version_id: str, key: str, limit: int) -> list[dict]:
    """First rows of `key` (index.parquet or data.parquet) in a committed version, read at its commit."""
    commit = await access.version_commit(tenant_id, dataset_id, version_id)
    location = f"{layout.repo_name(dataset_id)}/{commit}/{_checked_path(key)}"
    return await asyncio.to_thread(_parquet_head, secret_files.LAKEFS, location, limit)


def _parquet_column(secret: str, location: str, column: str) -> list:
    from pyarrow.parquet import ParquetFile  # noqa: PLC0415 - imported lazily, like _parquet_head

    bucket, _, key = location.partition("/")
    with io.BufferedReader(_S3Object(_s3(secret), bucket, key), _PARQUET_READ_BUFFER) as f:
        return ParquetFile(f).read(columns=[column]).column(column).to_pylist()


async def read_version_column(tenant_id: str, dataset_id: str, version_id: str, key: str, column: str) -> list:
    """Every value of one column of `key` (index.parquet or data.parquet) in a committed version."""
    commit = await access.version_commit(tenant_id, dataset_id, version_id)
    location = f"{layout.repo_name(dataset_id)}/{commit}/{_checked_path(key)}"
    return await asyncio.to_thread(_parquet_column, secret_files.LAKEFS, location, column)


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


def _staged_violation(entries: list[tuple[str, int]], limits: DatasetLimits) -> str | None:
    """Why `out/` may not be imported, or None. The job is untrusted input: what it wrote is checked here, not believed."""
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


async def _link_staged(client: httpx.AsyncClient, s3, repo: str, staged_prefix: str, entries: list[tuple[str, int]]) -> None:
    """Copy each staged object into the repo's storage namespace and link it on main, uncommitted.

    The copy runs inside the object store (S3 CopyObject), so no bytes pass through core-api, and the version no
    longer depends on staging: a lakeFS import would only point at the staged objects.
    """
    backing = f"/repositories/{repo}/branches/{layout.MAIN_BRANCH}/staging/backing"
    gate = asyncio.Semaphore(LINK_CONCURRENCY)

    async def link(rel: str, size: int) -> None:
        async with gate:
            placed = await client.get(backing, params={"path": rel})
            placed.raise_for_status()
            address = placed.json()["physical_address"]  # carries lakeFS's token after a comma, and is the object's key as is
            target = urlparse(address)
            copied = await asyncio.to_thread(
                s3.copy_object, Bucket=target.netloc, Key=target.path.lstrip("/"),
                CopySource={"Bucket": settings.data_plane.staging_bucket, "Key": staged_prefix + rel},
            )
            linked = await client.put(
                backing, params={"path": rel},
                json={"staging": {"physical_address": address}, "checksum": copied["CopyObjectResult"]["ETag"].strip('"'), "size_bytes": size},
            )
            linked.raise_for_status()

    await asyncio.gather(*(link(rel, size) for rel, size in entries))


async def commit_staged(tenant_id: str, dataset_id: str, import_id: str, message: str, limits: DatasetLimits) -> str:
    """Put the staged `out/` tree on the repo's main branch as one commit; returns the commit id.

    What is staged is checked first (the tenant's plan `limits`, allowed file names), then copied inside the object store into the
    repo's own storage and linked, so the version outlives staging. The commit carries the import id, so calling
    this again for the same import after a failure returns the commit already made instead of making a second one.
    """
    await access.dataset_import(tenant_id, dataset_id, import_id)
    prefix = layout.staging_prefix(tenant_id, dataset_id, import_id)
    repo = layout.repo_name(dataset_id)
    async with _lakefs_api() as client:
        if (done := await _existing_commit(client, repo, import_id)) is not None:
            return done
        entries = await asyncio.to_thread(_list_staged, prefix + layout.OUT_PREFIX)
        if violation := _staged_violation(entries, limits):
            raise Problem(_HTTP_UNPROCESSABLE, INVALID_STAGED_OUTPUT, violation)
        async with asyncio.timeout(settings.data_plane.commit_timeout_seconds):
            await _link_staged(client, _s3(secret_files.STAGING), repo, prefix + layout.OUT_PREFIX, entries)
            committed = await client.post(
                f"/repositories/{repo}/branches/{layout.MAIN_BRANCH}/commits",
                json={"message": message, "metadata": {IMPORT_ID_METADATA: import_id}},
            )
        committed.raise_for_status()
        return committed.json()["id"]


def _delete_prefix(bucket: str, prefix: str, client) -> None:
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if keys:
            client.delete_objects(Bucket=bucket, Delete={"Objects": keys})


async def purge_staging(tenant_id: str, dataset_id: str, import_id: str) -> None:
    await access.dataset_import(tenant_id, dataset_id, import_id)
    prefix = layout.staging_prefix(tenant_id, dataset_id, import_id)
    await asyncio.to_thread(_delete_prefix, settings.data_plane.staging_bucket, prefix, _s3(secret_files.STAGING))
