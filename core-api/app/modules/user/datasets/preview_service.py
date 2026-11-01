"""Preview and files, served from the parquet index plus presigned GETs; lakeFS is never listed."""
import asyncio
import uuid
from http import HTTPStatus

from app.core import task_catalog
from app.core.authz import AuthzContext
from app.core.enums import TaskType
from app.core.problem import Problem, not_found
from app.core.tenant_conn import TenantConn
from app.integrations import data_plane
from app.integrations.data_plane import layout
from app.modules.user.datasets.constants import ImportStatus, ProblemCode
from app.modules.user.datasets.readers import dataset_task
from app.modules.user.datasets.schemas import DatasetPreview, FileLink, PreviewRow

PRESIGN_CONCURRENCY = 5  # each presign checks ownership on a pooled connection: bound them so a page cannot take the pool
_VERSION_EXISTS = "SELECT 1 FROM dataset_versions WHERE id = $1 AND dataset_id = $2"
_IMPORT_STATUS = "SELECT status FROM dataset_imports WHERE id = $1 AND dataset_id = $2"
_NOT_IN_REVIEW = "only an import awaiting review has a staged preview"


async def _task(conn, project_id: uuid.UUID, dataset_id: uuid.UUID) -> TaskType:
    task = await dataset_task(conn, project_id, dataset_id)
    if task is None:  # a project without a task type has no data to preview
        raise not_found()
    return task


async def _version_task(conn, project_id: uuid.UUID, dataset_id: uuid.UUID, version_id: uuid.UUID) -> TaskType:
    task = await _task(conn, project_id, dataset_id)
    if await conn.fetchval(_VERSION_EXISTS, version_id, dataset_id) is None:
        raise not_found()
    return task


def _key(task: TaskType) -> str:
    return layout.DATA_KEY if task_catalog.is_table(task) else layout.INDEX_KEY


async def version_preview(
    ctx: AuthzContext, project_id: uuid.UUID, dataset_id: uuid.UUID, version_id: uuid.UUID, limit: int
) -> DatasetPreview:
    async with TenantConn(ctx.tenant_id) as conn:
        task = await _version_task(conn, project_id, dataset_id, version_id)
    tenant, ds, ver = ctx.tenant_id, str(dataset_id), str(version_id)
    rows = await data_plane.read_version_sample(tenant, ds, ver, _key(task), limit)
    if task_catalog.is_table(task):
        return DatasetPreview(rows=[PreviewRow(data=r) for r in rows])
    # ponytail: one ownership query per URL, PRESIGN_CONCURRENCY at a time; add a batch presign op if previews grow past a page
    gate = asyncio.Semaphore(PRESIGN_CONCURRENCY)

    async def presign(row: dict) -> str:
        async with gate:
            return await data_plane.presign_get(tenant, ds, ver, layout.OBJECTS_PREFIX + row["path"])

    urls = await asyncio.gather(*(presign(r) for r in rows))
    return DatasetPreview(rows=[PreviewRow(data=r, url=u) for r, u in zip(rows, urls)])


async def import_preview(
    ctx: AuthzContext, project_id: uuid.UUID, dataset_id: uuid.UUID, import_id: uuid.UUID, limit: int
) -> DatasetPreview:
    """Review preview: the staged index. No image URLs, because a merge import's staged tree holds only the new objects."""
    async with TenantConn(ctx.tenant_id) as conn:
        task = await _task(conn, project_id, dataset_id)
        status = await conn.fetchval(_IMPORT_STATUS, import_id, dataset_id)
    if status is None:
        raise not_found()
    if status != ImportStatus.AWAITING_REVIEW:
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.INVALID_STATE, _NOT_IN_REVIEW)
    rows = await data_plane.read_staged_sample(ctx.tenant_id, str(dataset_id), str(import_id), _key(task), limit)
    return DatasetPreview(rows=[PreviewRow(data=r) for r in rows])


async def file_link(
    ctx: AuthzContext, project_id: uuid.UUID, dataset_id: uuid.UUID, version_id: uuid.UUID, path: str
) -> FileLink:
    """Presigned GET for one image of a version; `path` is the index's `path` column."""
    async with TenantConn(ctx.tenant_id) as conn:
        await _version_task(conn, project_id, dataset_id, version_id)
    url = await data_plane.presign_get(ctx.tenant_id, str(dataset_id), str(version_id), layout.OBJECTS_PREFIX + path)
    return FileLink(url=url)
