"""The platform-wide activity log: every tenant's audit trail, interleaved.

The user-facing API already serves one project's trail
(`GET /v1/projects/{id}/audit-log`, app/modules/user/projects/routes.py).
This is the same table read the other way round — across every tenant at
once, which is the operator's view and nobody else's, so it goes through
get_platform_admin and a SECURITY DEFINER function (admin_activity()) rather than the
RLS-scoped connection that route uses. Where each field comes from: service.py.
"""
import uuid

from fastapi import APIRouter, Depends, Query, Response

from app.core.auth import get_platform_admin
from app.modules.admin.activity import service
from app.modules.admin.activity.schemas import ActivityItem, ActivityStats

router = APIRouter(prefix="/v1/admin/activity", tags=["admin"])


@router.get("", dependencies=[Depends(get_platform_admin)], response_model=list[ActivityItem])
async def list_activity(
    response: Response,
    q: str | None = Query(default=None, description="matches actor, action, entity, tenant or project"),
    tenant_id: uuid.UUID | None = Query(default=None, description="one organisation only"),
    entity_type: str | None = Query(default=None, description="project | project_member | ..."),
    days: int | None = Query(default=None, ge=1, le=3650, description="only the last N days"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Every recorded action, newest first.

    `X-Total-Count` carries the size of the filtered set so a pager costs
    no second request — the same contract as `GET /v1/admin/tenants`."""
    rows, total = await service.list_activity(q, tenant_id, entity_type, days, limit, offset)
    response.headers["X-Total-Count"] = str(total)
    return rows


@router.get("/summary", dependencies=[Depends(get_platform_admin)], response_model=ActivityStats)
async def activity_summary():
    """The totals behind the log's stat strip."""
    return await service.summary()
