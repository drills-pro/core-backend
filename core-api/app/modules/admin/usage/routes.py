"""The platform-wide usage ledger: every tenant's usage_events, interleaved.

The user-facing API already serves one project's raw events
(`GET /v1/projects/{id}/usage`, app/modules/user/usage). This is the same
table read across every tenant at once, which is the operator's view and
nobody else's, so it goes through get_platform_admin and a SECURITY
DEFINER function (0038) rather than the RLS-scoped connection that route
uses — the same arrangement as the audit trail in app/modules/admin/activity.
"""
import datetime
import uuid

from fastapi import APIRouter, Depends, Query, Response

from app.core.auth import get_platform_admin
from app.core.db import get_pool

router = APIRouter(prefix="/v1/admin/usage-events", tags=["admin"])


@router.get("", dependencies=[Depends(get_platform_admin)])
async def list_usage_events(
    response: Response,
    q: str | None = Query(default=None, description="matches resource type, source, tenant or project"),
    tenant_id: uuid.UUID | None = Query(default=None, description="one organisation only"),
    resource_type: str | None = Query(
        default=None, description="training | inference | storage_dataset | storage_model"
    ),
    days: int | None = Query(default=None, ge=1, le=3650, description="only the last N days"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Every metered event, newest first.

    `X-Total-Count` carries the size of the filtered set so a pager costs
    no second request — the same contract as `GET /v1/admin/activity`."""
    since = None
    if days is not None:
        since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)

    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM admin_usage_events($1, $2, $3, $4, $5, $6)",
            limit,
            offset,
            tenant_id,
            resource_type,
            q,
            since,
        )
    # count(*) OVER () has no row to ride on when the filter matches
    # nothing, which is the one case the header has to be told about.
    response.headers["X-Total-Count"] = str(rows[0]["total"] if rows else 0)
    # `total` is an implementation detail of the pager, not part of a row.
    return [{key: value for key, value in row.items() if key != "total"} for row in rows]


@router.get("/summary", dependencies=[Depends(get_platform_admin)])
async def usage_events_summary():
    """The totals behind the log's stat strip, counted on the database's
    day so they agree with what the listing groups under Today."""
    async with (await get_pool()).acquire() as conn:
        stats = await conn.fetchrow("SELECT * FROM admin_usage_stats()")
    return dict(stats)
