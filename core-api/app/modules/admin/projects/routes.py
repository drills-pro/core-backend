"""Every project on the platform, across all tenants.

The user-facing listing is tenant-scoped (`/v1/projects`,
app/modules/user/projects) and the tenant detail page lists one tenant's
projects (0027). This is every project side by side, which is the
operator's view alone, so it goes through get_platform_admin and the
SECURITY DEFINER functions in 0033 rather than an RLS-scoped connection.

Each row carries what the back-office draws beside a project: its
organisation, how many datasets and models it holds, its newest model, and
whether anything of it is serving — derived from `deployments`, in the
same deployed / deploying / stopped / not_deployed vocabulary as
/v1/admin/models.

`GET /{id}` and the four readers under it are one project opened from that
listing: its header, then its models, datasets, deployments and members,
each its own request so the page can load them on separate boundaries.
"""
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.core.auth import get_platform_admin
from app.core.db import get_pool
from app.integrations import zitadel
from app.modules.admin.models.routes import TASK_TYPES, VISIBILITIES, reject_unknown
from app.modules.admin.tenants.detail import UNKNOWN_USER
from app.modules.admin.tenants.routes import ZITADEL_TIMEOUT

router = APIRouter(prefix="/v1/admin/projects", tags=["admin"])


@router.get("", dependencies=[Depends(get_platform_admin)])
async def list_projects(
    response: Response,
    q: str | None = Query(default=None, description="matches project or organisation"),
    tenant_id: uuid.UUID | None = Query(default=None, description="one organisation only"),
    task_type: str | None = Query(default=None, description="image_classification | ..."),
    visibility: str | None = Query(default=None, description="private | public"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Every live project, newest first.

    `X-Total-Count` carries the size of the filtered set so a pager costs
    no second request — the same contract as the other admin listings."""
    reject_unknown("task_type", task_type, TASK_TYPES)
    reject_unknown("visibility", visibility, VISIBILITIES)

    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM admin_projects($1, $2, $3, $4, $5, $6)",
            limit,
            offset,
            tenant_id,
            task_type,
            visibility,
            q,
        )
    response.headers["X-Total-Count"] = str(rows[0]["total"] if rows else 0)
    return [{k: v for k, v in dict(row).items() if k != "total"} for row in rows]


@router.get("/summary", dependencies=[Depends(get_platform_admin)])
async def projects_summary():
    """The totals behind the stat strip, counted across every tenant."""
    async with (await get_pool()).acquire() as conn:
        stats = await conn.fetchrow("SELECT * FROM admin_project_stats()")
    return dict(stats)


# --- One project -------------------------------------------------------------
# Registered after /summary, so that path is never read as a project id.


@router.get("/{project_id}", dependencies=[Depends(get_platform_admin)])
async def get_project(project_id: uuid.UUID):
    """The header of the detail page: identity, organisation, counts."""
    async with (await get_pool()).acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM admin_project($1)", project_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such project")
    return {k: v for k, v in dict(row).items() if k != "zitadel_org_id"}


@router.get("/{project_id}/models", dependencies=[Depends(get_platform_admin)])
async def list_project_models(project_id: uuid.UUID):
    """Every registered version in the project, newest first."""
    return await _rows("SELECT * FROM admin_project_models($1)", project_id)


@router.get("/{project_id}/datasets", dependencies=[Depends(get_platform_admin)])
async def list_project_datasets(project_id: uuid.UUID):
    return await _rows("SELECT * FROM admin_project_datasets($1)", project_id)


@router.get("/{project_id}/deployments", dependencies=[Depends(get_platform_admin)])
async def list_project_deployments(project_id: uuid.UUID):
    """Every deployment the project has had, failed and deleted included."""
    return await _rows("SELECT * FROM admin_project_deployments($1)", project_id)


@router.get("/{project_id}/members", dependencies=[Depends(get_platform_admin)])
async def list_project_members(project_id: uuid.UUID):
    """Who has access to the project, joined onto Zitadel for their names.

    Degrades the way the tenant's member list does: a person Zitadel will
    not answer for keeps their row, with the bare user id and "unknown"."""
    async with (await get_pool()).acquire() as conn:
        org_id = await conn.fetchval("SELECT zitadel_org_id FROM admin_project($1)", project_id)
        rows = await conn.fetch("SELECT * FROM admin_project_members($1)", project_id)
    if not rows:
        return []

    async with httpx.AsyncClient(timeout=ZITADEL_TIMEOUT) as client:
        users = await zitadel.list_org_users(client, org_id)
    return [dict(row, **(users.get(row["user_id"]) or UNKNOWN_USER)) for row in rows]


async def _rows(sql: str, project_id: uuid.UUID) -> list[dict]:
    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch(sql, project_id)
    return [dict(row) for row in rows]
