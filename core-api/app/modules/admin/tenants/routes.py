"""Cross-tenant listing of every tenant on the platform (req/01 FR1 is
about isolating tenants from *each other* — the operator's back-office is
the one audience allowed to see them side by side).

One row is assembled from the three systems that each own part of it:

    tenants          name, plan tier, created_at      (Postgres, no RLS —
                     core_app has a plain GRANT SELECT, init migration)
    admin_tenant_stats()  project/member counts       (Postgres, SECURITY
                     DEFINER: those tables *are* RLS-protected and this
                     connection sets no app.tenant_id, so a direct COUNT
                     would return 0 for everyone — admin_tenant_stats())
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
import logging

import asyncpg
import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response

from app.core.auth import get_platform_admin
from app.core.db import get_pool
from app.core.tenant_conn import TenantConn
from app.integrations import zitadel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/tenants", tags=["admin"])

# The plan tiers tenants.plan_tier accepts (init migration).
PLAN_TIERS = ("free", "pro", "enterprise")

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


@router.post("", status_code=201)
async def create_tenant(
    name: str = Body(..., embed=True),
    plan_tier: str = Body(default="free", embed=True),
    zitadel_org_id: str | None = Body(default=None, embed=True),
    _actor: str = Depends(get_platform_admin),
):
    """Provision an organisation.

    Three steps, in the order scripts/zitadel-bootstrap.sh does them by
    hand: the Zitadel org, the tenant row that maps to it, and the Default
    team every tenant is assumed to have (FR1 — project creation
    falls back to it, so a tenant without one cannot hold a project).

    `zitadel_org_id` adopts an org that already exists rather than making
    a new one, which is the case the bootstrap script covers: the org came
    first and only the platform-side mapping is missing.

    Zitadel and Postgres cannot be written in one transaction. The org
    goes first, because a tenant row pointing at an org that does not
    exist is the worse half-state — every listing would show it as
    "unknown" forever. If the row then fails to land, an org this request
    created is deleted again on the way out; an adopted one is left alone,
    since it was not this request's to remove.
    """
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if plan_tier not in PLAN_TIERS:
        raise HTTPException(status_code=400, detail=f"plan_tier must be one of {PLAN_TIERS}")

    async with httpx.AsyncClient(timeout=ZITADEL_TIMEOUT) as client:
        adopted = zitadel_org_id is not None
        if not adopted:
            try:
                zitadel_org_id = await zitadel.create_org(client, name)
            except httpx.HTTPStatusError as error:
                raise zitadel_refused(error) from error
            except (httpx.HTTPError, OSError) as error:
                raise HTTPException(
                    status_code=502, detail=f"zitadel unreachable: {error}"
                ) from error

        try:
            tenant = await _insert_tenant(zitadel_org_id, name, plan_tier)
        except Exception:
            if not adopted:
                await _discard_org(client, zitadel_org_id)
            raise

    # A freshly created org is active, and an adopted one is whatever
    # Zitadel already says it is — but that is one more round trip for a
    # value the caller is about to read from the listing anyway.
    return dict(tenant, status="active" if not adopted else "unknown")


async def _insert_tenant(zitadel_org_id: str, name: str, plan_tier: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                "INSERT INTO tenants (zitadel_org_id, name, plan_tier) VALUES ($1, $2, $3) "
                "RETURNING id, name, zitadel_org_id, plan_tier, created_at",
                zitadel_org_id,
                name,
                plan_tier,
            )
        except asyncpg.UniqueViolationError as error:
            raise HTTPException(
                status_code=409,
                detail=f"org {zitadel_org_id} is already mapped to a tenant",
            ) from error

    # teams is RLS-protected, so the Default team is written as the tenant
    # rather than around it — the same connection its own members get.
    async with TenantConn(str(row["id"])) as conn:
        await conn.execute(
            "INSERT INTO teams (tenant_id, name) VALUES ($1, 'Default')", row["id"]
        )
    return dict(row)


async def _discard_org(client: httpx.AsyncClient, org_id: str) -> None:
    """Best effort: an org nobody can reach through this platform is
    better deleted, but failing to delete it must not replace the error
    that got us here."""
    try:
        await zitadel.delete_org(client, org_id)
    except (httpx.HTTPError, OSError):
        logger.warning(
            "left an orphan zitadel org %s: its tenant row failed to insert",
            org_id,
            exc_info=True,
        )


def zitadel_refused(error: httpx.HTTPStatusError) -> HTTPException:
    """Zitadel's own refusal, at the status the caller should see.

    Its message says what happened — the org name is taken, the org is
    already deactivated, it doesn't exist — better than anything this
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
    happens in Postgres (admin_platform_stats()) rather than over the
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
