"""The platform-wide activity log: every tenant's audit trail, interleaved.

The user-facing API already serves one project's trail
(`GET /v1/projects/{id}/audit-log`, app/modules/user/projects/routes.py).
This is the same table read the other way round — across every tenant at
once, which is the operator's view and nobody else's, so it goes through
get_platform_admin and a SECURITY DEFINER function (admin_activity()) rather than the
RLS-scoped connection that route uses.

Two sources per row:

    admin_activity()  the event, plus the tenant and project it names
                      (Postgres, SECURITY DEFINER: audit_log is
                      RLS-protected and this connection sets no
                      app.tenant_id, so a direct SELECT returns nothing)
    Zitadel           who the actor is (audit_log stores the subject id,
                      and Zitadel owns the name behind it)

Zitadel is enrichment, not a dependency: if it is unreachable the log
still answers, with actor_name=null, and the dashboard shows the bare
subject id rather than nothing at all.
"""
import asyncio
import datetime
import uuid

import httpx
from fastapi import APIRouter, Depends, Query, Response

from app.core.auth import get_platform_admin
from app.core.db import get_pool
from app.integrations import zitadel

router = APIRouter(prefix="/v1/admin/activity", tags=["admin"])

# One Zitadel user search per org on the page (there is no cross-org
# endpoint), so the ceiling is the number of distinct tenants in a page,
# not the number of events. Same budget as the tenant listing's owner
# lookups, kept local rather than imported: a constant is not a public API.
ORG_CONCURRENCY = 8
ZITADEL_TIMEOUT = 10


@router.get("", dependencies=[Depends(get_platform_admin)])
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
    since = None
    if days is not None:
        since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM admin_activity($1, $2, $3, $4, $5, $6)",
            limit,
            offset,
            tenant_id,
            entity_type,
            q,
            since,
        )
        # count(*) OVER () has no row to ride on when the filter matches
        # nothing, which is the one case the header has to be told about.
        response.headers["X-Total-Count"] = str(rows[0]["total"] if rows else 0)
        orgs = await _org_ids(conn, {row["tenant_id"] for row in rows})

    names = await _actor_names(orgs)
    return [
        {
            key: value
            for key, value in dict(row, actor_name=names.get((row["tenant_id"], row["actor"]))).items()
            # An implementation detail of the pager, not part of a row.
            if key != "total"
        }
        for row in rows
    ]


@router.get("/summary", dependencies=[Depends(get_platform_admin)])
async def activity_summary():
    """The totals behind the log's stat strip.

    `events_today` counts the database's day — the clock that stamped the
    rows — so it always agrees with what the listing groups under Today.
    """
    async with (await get_pool()).acquire() as conn:
        stats = await conn.fetchrow("SELECT * FROM admin_activity_stats()")
    return dict(stats)


async def _org_ids(conn, tenant_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """{tenant_id: zitadel_org_id} for the tenants this page names.

    tenants is the one table here with no RLS on it (init migration), so this is a
    plain SELECT rather than another SECURITY DEFINER hop.
    """
    if not tenant_ids:
        return {}
    rows = await conn.fetch(
        "SELECT id, zitadel_org_id FROM tenants WHERE id = ANY($1::uuid[])", list(tenant_ids)
    )
    return {row["id"]: row["zitadel_org_id"] for row in rows}


async def _actor_names(orgs: dict[uuid.UUID, str]) -> dict[tuple[uuid.UUID, str], str]:
    """{(tenant_id, subject): display name} for everyone in those orgs.

    Keyed by tenant as well as subject because the same person can hold
    accounts in several orgs, and it is that org's Zitadel that names them.
    Failures are per-org and silent: `zitadel.list_org_users` already logs
    them and answers {}, which leaves those rows with their subject id.
    """
    if not orgs:
        return {}

    semaphore = asyncio.Semaphore(ORG_CONCURRENCY)
    async with httpx.AsyncClient(timeout=ZITADEL_TIMEOUT) as client:

        async def one(org_id: str) -> dict[str, dict]:
            async with semaphore:
                return await zitadel.list_org_users(client, org_id)

        results = await asyncio.gather(*(one(org_id) for org_id in orgs.values()))

    return {
        (tenant_id, subject): user["display_name"]
        for (tenant_id, _), users in zip(orgs.items(), results)
        for subject, user in users.items()
        if user["display_name"]
    }
