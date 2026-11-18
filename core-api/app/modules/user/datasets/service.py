"""Dataset rules and queries: create, list, get, soft delete / restore, versions."""
import uuid

from app.core import audit
from app.core.authz import AuthzContext
from app.core.metrics import usage
from app.core.pagination import Page, PageParams, SortDir, build_page, keyset_clause, order_by
from app.core.problem import Problem, not_found
from app.core.tenant_conn import TenantConn
from app.modules.user.datasets import import_service
from app.modules.user.datasets.constants import (
    DATASET_RETENTION_DAYS,
    NOT_DELETED_DETAIL,
    AuditAction,
    DataCategory,
    EntityType,
    ProblemCode,
)
from app.modules.user.datasets.schemas import DatasetSummary, DatasetVersion, TrashedDataset
from app.modules.user.projects import ProjectStatus, get_task_type

_COLUMNS = "id, name, created_at, deleted_at, published_version_id, data_categories"


async def _summary(conn, project_id: uuid.UUID, row) -> DatasetSummary:
    """The dataset with its project's task type (one task type per project, never stored on the dataset)."""
    return DatasetSummary.model_validate({**dict(row), "task_type": await get_task_type(conn, project_id)})


async def create_dataset(ctx: AuthzContext, project_id: uuid.UUID, name: str, data_categories: list[DataCategory]) -> DatasetSummary:
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "INSERT INTO datasets (tenant_id, project_id, name, data_categories) VALUES ($2, $1, $3, $4) "
            f"RETURNING {_COLUMNS}",
            project_id,
            ctx.tenant_id,
            name,
            [str(c) for c in data_categories],
        )
        await audit.record_for(conn, ctx, project_id, AuditAction.CREATE, EntityType.DATASET, row["id"])
        return await _summary(conn, project_id, row)


async def list_datasets(ctx: AuthzContext, project_id: uuid.UUID, page: PageParams) -> Page[DatasetSummary]:
    async with TenantConn(ctx.tenant_id) as conn:
        cond, args = keyset_clause(page.page_token, SortDir.DESC, first_param=2)
        rows = await conn.fetch(
            f"SELECT {_COLUMNS} FROM datasets WHERE project_id = $1 AND deleted_at IS NULL {cond} "
            f"ORDER BY {order_by(SortDir.DESC)} LIMIT {page.page_size + 1}",
            project_id,
            *args,
        )
        task_type = await get_task_type(conn, project_id)
    return build_page([{**dict(r), "task_type": task_type} for r in rows], page.page_size, DatasetSummary)


async def list_trashed_datasets(ctx: AuthzContext, page: PageParams) -> Page[TrashedDataset]:
    """Deleted datasets still inside the retention window, across the projects the caller can see (a deleted
    project's datasets come back with the project)."""
    async with TenantConn(ctx.tenant_id) as conn:
        ids = await ctx.accessible_project_ids(conn)
        cond, args = keyset_clause(page.page_token, SortDir.DESC, first_param=4)
        rows = await conn.fetch(
            "SELECT id, name, created_at, deleted_at, project_id, "
            "(SELECT name FROM projects WHERE id = project_id) AS project_name FROM datasets "
            "WHERE project_id = ANY($1) AND deleted_at > now() - make_interval(days => $2) "
            "AND project_id NOT IN (SELECT id FROM projects WHERE status = $3) "
            f"{cond} ORDER BY {order_by(SortDir.DESC)} LIMIT {page.page_size + 1}",
            ids,
            DATASET_RETENTION_DAYS,
            ProjectStatus.PENDING_DELETE,
            *args,
        )
    return build_page(rows, page.page_size, TrashedDataset)


async def get_dataset(ctx: AuthzContext, project_id: uuid.UUID, dataset_id: uuid.UUID) -> DatasetSummary:
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            f"SELECT {_COLUMNS} FROM datasets WHERE project_id = $1 AND id = $2 AND deleted_at IS NULL",
            project_id,
            dataset_id,
        )
        if row is None:  # RLS hides other tenants' rows: indistinguishable from a missing one
            raise not_found()
        return await _summary(conn, project_id, row)


async def delete_dataset(ctx: AuthzContext, project_id: uuid.UUID, dataset_id: uuid.UUID) -> DatasetSummary:
    """Soft delete; a sweep (plan step 8) removes the lakeFS repo and rows after the retention window. Imports whose
    job is still queued or running are cancelled now: a job would otherwise keep writing to staging unobserved."""
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "UPDATE datasets SET deleted_at = now() WHERE project_id = $1 AND id = $2 AND deleted_at IS NULL "
            f"RETURNING {_COLUMNS}",
            project_id,
            dataset_id,
        )
        if row is None:
            raise not_found()
        await audit.record_for(conn, ctx, project_id, AuditAction.DELETE, EntityType.DATASET, dataset_id)
        cancelled = await import_service.cancel_running_imports(conn, dataset_id)
        summary = await _summary(conn, project_id, row)
    await import_service.finish_cancelled(cancelled)
    return summary


async def restore_dataset(ctx: AuthzContext, project_id: uuid.UUID, dataset_id: uuid.UUID) -> DatasetSummary:
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "UPDATE datasets SET deleted_at = NULL WHERE project_id = $1 AND id = $2 AND deleted_at IS NOT NULL "
            "AND deleted_at > now() - make_interval(days => $3) "
            f"RETURNING {_COLUMNS}",
            project_id,
            dataset_id,
            DATASET_RETENTION_DAYS,
        )
        if row is None:
            if await conn.fetchval("SELECT 1 FROM datasets WHERE project_id = $1 AND id = $2", project_id, dataset_id) is None:
                raise not_found()
            raise Problem(409, ProblemCode.NOT_DELETED, NOT_DELETED_DETAIL)
        await audit.record_for(conn, ctx, project_id, AuditAction.RESTORE, EntityType.DATASET, dataset_id)
        return await _summary(conn, project_id, row)


async def list_versions(
    ctx: AuthzContext, project_id: uuid.UUID, dataset_id: uuid.UUID, page: PageParams
) -> Page[DatasetVersion]:
    async with TenantConn(ctx.tenant_id) as conn:
        if await conn.fetchval(
            "SELECT 1 FROM datasets WHERE project_id = $1 AND id = $2 AND deleted_at IS NULL", project_id, dataset_id
        ) is None:
            raise not_found()
        cond, args = keyset_clause(page.page_token, SortDir.DESC, first_param=2)
        rows = await conn.fetch(
            "SELECT id, number, import_id, stats, created_at FROM dataset_versions "
            f"WHERE dataset_id = $1 {cond} ORDER BY {order_by(SortDir.DESC)} LIMIT {page.page_size + 1}",
            dataset_id,
            *args,
        )
    return build_page(rows, page.page_size, DatasetVersion)


async def publish_version(ctx: AuthzContext, project_id: uuid.UUID, dataset_id: uuid.UUID, version_id: uuid.UUID) -> DatasetSummary:
    """Publish one of this dataset's versions (metadata and stats only); a version of another dataset is a 404."""
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "UPDATE datasets SET published_version_id = $3 WHERE project_id = $1 AND id = $2 AND deleted_at IS NULL "
            "AND EXISTS (SELECT 1 FROM dataset_versions WHERE id = $3 AND dataset_id = $2) "
            f"RETURNING {_COLUMNS}",
            project_id,
            dataset_id,
            version_id,
        )
        if row is None:
            raise not_found()
        await audit.record_for(conn, ctx, project_id, AuditAction.PUBLISH, EntityType.DATASET, dataset_id)
        summary = await _summary(conn, project_id, row)
    usage.datasets_published.add(1)
    return summary


async def unpublish_dataset(ctx: AuthzContext, project_id: uuid.UUID, dataset_id: uuid.UUID) -> DatasetSummary:
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "UPDATE datasets SET published_version_id = NULL WHERE project_id = $1 AND id = $2 AND deleted_at IS NULL "
            f"RETURNING {_COLUMNS}",
            project_id,
            dataset_id,
        )
        if row is None:
            raise not_found()
        await audit.record_for(conn, ctx, project_id, AuditAction.UNPUBLISH, EntityType.DATASET, dataset_id)
        return await _summary(conn, project_id, row)
