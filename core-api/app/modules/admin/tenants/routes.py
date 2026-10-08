"""Cross-tenant listing of every tenant on the platform (req/01 FR1 is
about isolating tenants from *each other* — the operator's back-office is
the one audience allowed to see them side by side).

One row is assembled from the three systems that each own part of it:

    tenants          name, plan tier, created_at      (Postgres, no RLS —
                     core_app has a plain GRANT SELECT, 0001_tenancy.sql)
    admin_tenant_stats()  project/member counts       (Postgres, SECURITY
                     DEFINER: those tables *are* RLS-protected and this
                     connection sets no app.tenant_id, so a direct COUNT
                     would return 0 for everyone — 0025)
    Zitadel          owner and active/suspended       (the org is the
                     source of truth for both, req/02 FR2)

Zitadel is enrichment, not a dependency: if it is unreachable the list
still answers, with owner=null and status="unknown".

`GET /summary` alongside it is the same three systems asked for platform
totals instead of rows — the stat strip above the listing.
"""
import asyncio
import collections
import json

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.core.auth import get_platform_admin
from app.core.db import get_pool
from app.integrations import zitadel

router = APIRouter(prefix="/v1/admin/tenants", tags=["admin"])

# Status lives in Zitadel, so it can only be filtered on after enrichment —
# which means the status-filtered page has to be cut from the whole matching
# set, not from one LIMIT'd window of it. This caps how much of that set a
# single request will scan; the platform has tenants in the tens, and a
# listing that silently truncates is worse than one that says it did.
MAX_SCAN = 1000
# Owner lookups are one Zitadel call each (there is no bulk endpoint), so
# they run only for the rows actually being returned, a few at a time.
OWNER_CONCURRENCY = 8
ZITADEL_TIMEOUT = 10


@router.get("", dependencies=[Depends(get_platform_admin)])
async def list_tenants(
    response: Response,
    q: str | None = Query(default=None, description="case-insensitive match on tenant name"),
    plan_tier: str | None = Query(default=None, description="free | pro | enterprise"),
    status: str | None = Query(default=None, description="active | suspended | unknown"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Every tenant, newest last.

    `X-Total-Count` carries the size of the filtered set and
    `X-Status-Counts` its active/suspended/unknown breakdown, so a pager
    and status tabs cost no extra requests."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT t.id, t.name, t.zitadel_org_id, t.plan_tier, t.created_at, "
            "       s.project_count, s.member_count "
            "FROM tenants t "
            # LATERAL so the counters run for the rows this filter keeps,
            # not for every tenant on the platform.
            "JOIN LATERAL admin_tenant_stats(ARRAY[t.id]) s ON true "
            "WHERE ($1::text IS NULL OR t.name ILIKE '%' || $1 || '%') "
            "AND ($2::text IS NULL OR t.plan_tier = $2) "
            "ORDER BY t.created_at, t.id LIMIT $3",
            q,
            plan_tier,
            MAX_SCAN,
        )

    async with httpx.AsyncClient(timeout=ZITADEL_TIMEOUT) as client:
        org_states = await zitadel.list_org_states(client)
        tenants = [
            dict(row, status=org_states.get(row["zitadel_org_id"], "unknown")) for row in rows
        ]
        # Before the status filter, so a status-filtered request still knows
        # what the other tabs hold and a back-office UI doesn't have to ask
        # three times to label them.
        response.headers["X-Status-Counts"] = json.dumps(
            collections.Counter(t["status"] for t in tenants)
        )
        if status is not None:
            tenants = [t for t in tenants if t["status"] == status]

        response.headers["X-Total-Count"] = str(len(tenants))
        page = tenants[offset : offset + limit]
        for tenant, owner in zip(page, await _owners(client, page)):
            tenant["owner"] = owner
    return page


def zitadel_refused(error: httpx.HTTPStatusError) -> HTTPException:
    """Zitadel's own refusal, at the status the caller should see.

    Its message says what happened — the org is already deactivated, it
    doesn't exist — better than anything this
    layer could infer from a status code.
    """
    try:
        detail = error.response.json().get("message", error.response.text)
    except ValueError:
        detail = error.response.text
    status = 409 if error.response.status_code < 500 else 502
    return HTTPException(status_code=status, detail=detail)


@router.get("/summary", dependencies=[Depends(get_platform_admin)])
async def tenants_summary():
    """The platform-wide totals behind the back-office stat cards.

    Deliberately unfiltered: the cards sit above the listing's tabs and
    filters and describe the platform, not the page under them. Counting
    happens in Postgres (admin_platform_stats(), 0026) rather than over the
    listing's rows, so the numbers don't stop at MAX_SCAN or at one page.

    `status_counts` is the same active/suspended/unknown breakdown the
    listing sends as `X-Status-Counts`, and degrades the same way: with
    Zitadel unreachable every organisation counts as unknown, which is
    honest about not knowing rather than reporting zero suspended.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        stats = await conn.fetchrow("SELECT * FROM admin_platform_stats()")
        org_ids = await conn.fetch("SELECT zitadel_org_id FROM tenants")

    async with httpx.AsyncClient(timeout=ZITADEL_TIMEOUT) as client:
        org_states = await zitadel.list_org_states(client)
    # Keyed off the tenants table, not off the Zitadel search: an org on the
    # instance with no tenant row (Zitadel's own instance org, for one) is
    # not an organisation this platform has.
    counts = collections.Counter(
        org_states.get(row["zitadel_org_id"], "unknown") for row in org_ids
    )

    return {
        "organizations": stats["tenant_count"],
        "organizations_new_30d": stats["tenant_count_30d"],
        "projects": stats["project_count"],
        "projects_new_30d": stats["project_count_30d"],
        "members": stats["member_count"],
        "status_counts": {
            status: counts.get(status, 0) for status in ("active", "suspended", "unknown")
        },
    }


async def _owners(client: httpx.AsyncClient, tenants: list[dict]) -> list[dict | None]:
    semaphore = asyncio.Semaphore(OWNER_CONCURRENCY)

    async def one(org_id: str):
        async with semaphore:
            return await zitadel.get_org_owner(client, org_id)

    return await asyncio.gather(*(one(t["zitadel_org_id"]) for t in tenants))
