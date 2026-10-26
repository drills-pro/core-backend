"""Ownership checks, run before every gateway operation touches lakeFS, S3 or MLflow.

Each check is one query as the calling tenant, with an explicit `tenant_id` match as well as RLS. A resource
that is missing, soft-deleted elsewhere or another tenant's is the same 404 (`not_found`), and nothing outside
Postgres has been called by then.
"""
import uuid

from app.core.problem import not_found
from app.core.tenant_conn import TenantConn

_DATASET = "SELECT 1 FROM datasets WHERE id = $1 AND tenant_id = $2"
_IMPORT = "SELECT 1 FROM dataset_imports WHERE id = $1 AND dataset_id = $2 AND tenant_id = $3"
_VERSION = "SELECT lakefs_commit FROM dataset_versions WHERE id = $1 AND dataset_id = $2 AND tenant_id = $3"
_PROJECT = "SELECT 1 FROM projects WHERE id = $1 AND tenant_id = $2"


async def _check(tenant_id: str, sql: str, *ids: str):
    try:
        args = [uuid.UUID(i) for i in (*ids, tenant_id)]
    except ValueError:
        raise not_found() from None
    async with TenantConn(tenant_id) as conn:
        value = await conn.fetchval(sql, *args)
    if value is None:
        raise not_found()
    return value


async def dataset(tenant_id: str, dataset_id: str) -> None:
    await _check(tenant_id, _DATASET, dataset_id)


async def dataset_import(tenant_id: str, dataset_id: str, import_id: str) -> None:
    await _check(tenant_id, _IMPORT, import_id, dataset_id)


async def version_commit(tenant_id: str, dataset_id: str, version_id: str) -> str:
    """The lakeFS commit of a version of this tenant's dataset."""
    return await _check(tenant_id, _VERSION, version_id, dataset_id)


async def project(tenant_id: str, project_id: str) -> None:
    await _check(tenant_id, _PROJECT, project_id)
