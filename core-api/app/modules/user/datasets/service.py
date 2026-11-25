"""Dataset rules and queries: create, list, get, soft delete / restore, versions."""
import uuid

import asyncpg

from app.core import audit
from app.core.authz import AuthzContext
from app.core.pagination import Page, PageParams, SortDir, build_page, keyset_clause, order_by
from app.core.problem import Problem, not_found
from app.core.tenant_conn import TenantConn
from app.modules.user.datasets.constants import (
    DATASET_EXISTS_DETAIL,
    AuditAction,
    EntityType,
    ProblemCode,
)
from app.modules.user.datasets.schemas import DatasetSummary, DatasetVersion
from app.modules.user.projects import get_task_type

_COLUMNS = "id, name, created_at"


async def _summary(conn, project_id: uuid.UUID, row) -> DatasetSummary:
    """The dataset with its project's task type (one task type per project, never stored on the dataset)."""
    return DatasetSummary.model_validate({**dict(row), "task_type": await get_task_type(conn, project_id)})


async def create_dataset(ctx: AuthzContext, project_id: uuid.UUID, name: str) -> DatasetSummary:
    async with TenantConn(ctx.tenant_id) as conn:
        try:
            row = await conn.fetchrow(
                "INSERT INTO datasets (tenant_id, project_id, name) VALUES ($2, $1, $3) "
                f"RETURNING {_COLUMNS}",
                project_id,
                ctx.tenant_id,
                name,
            )
        except asyncpg.UniqueViolationError:
            raise Problem(409, ProblemCode.DATASET_EXISTS, DATASET_EXISTS_DETAIL) from None
        await audit.record_for(conn, ctx, project_id, AuditAction.CREATE, EntityType.DATASET, row["id"])
        return await _summary(conn, project_id, row)


async def list_datasets(ctx: AuthzContext, project_id: uuid.UUID, page: PageParams) -> Page[DatasetSummary]:
    async with TenantConn(ctx.tenant_id) as conn:
        cond, args = keyset_clause(page.page_token, SortDir.DESC, first_param=2)
        rows = await conn.fetch(
            f"SELECT {_COLUMNS} FROM datasets WHERE project_id = $1 {cond} "
            f"ORDER BY {order_by(SortDir.DESC)} LIMIT {page.page_size + 1}",
            project_id,
            *args,
        )
        task_type = await get_task_type(conn, project_id)
    return build_page([{**dict(r), "task_type": task_type} for r in rows], page.page_size, DatasetSummary)


async def get_dataset(ctx: AuthzContext, project_id: uuid.UUID, dataset_id: uuid.UUID) -> DatasetSummary:
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            f"SELECT {_COLUMNS} FROM datasets WHERE project_id = $1 AND id = $2",
            project_id,
            dataset_id,
        )
        if row is None:  # RLS hides other tenants' rows: indistinguishable from a missing one
            raise not_found()
        return await _summary(conn, project_id, row)


async def list_versions(
    ctx: AuthzContext, project_id: uuid.UUID, dataset_id: uuid.UUID, page: PageParams
) -> Page[DatasetVersion]:
    async with TenantConn(ctx.tenant_id) as conn:
        if await conn.fetchval(
            "SELECT 1 FROM datasets WHERE project_id = $1 AND id = $2", project_id, dataset_id
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
