"""The back-office's view of tenants, assembled from the three systems that each own part of one:

    tenants          name, plan tier, created_at      (Postgres, no RLS —
                     core_app has a plain GRANT SELECT, init migration)
    admin_tenant_stats()  project/member counts       (Postgres, SECURITY
                     DEFINER: those tables *are* RLS-protected and this
                     connection sets no app.tenant_id, so a direct COUNT
                     would return 0 for everyone — admin_tenant_stats())
    Zitadel          owner and active/suspended       (the org is the
                     source of truth for both, req/02 FR2)

Zitadel is enrichment, not a dependency: if it is unreachable the reads
still answer, with owner=null and status="unknown".

The writes act *as* the tenant where its rows are RLS-protected: through
TenantConn, the same connection a user of that tenant would get, so a
mistyped id fails the insert instead of landing a row in the wrong tenant.
"""
import asyncio
import collections
import logging
import uuid

import asyncpg
import httpx
from fastapi import HTTPException
from pydantic import BaseModel

from app.core import task_types
from app.core.authz import TenantStatus
from app.core.db import get_pool
from app.core.enums import IdentityStatus, PlanTier, TaskType
from app.core.tenant_conn import TenantConn
from app.core.tenant_settings import TenantSettings
from app.integrations import zitadel
from app.modules.admin.tenants.constants import (
    ADMIN_SETTABLE_STATUSES,
    MAX_SCAN,
    NO_SUCH_TENANT,
    ORG_STATUSES,
    OWNER_CONCURRENCY,
    UNKNOWN_USER,
    ZITADEL_TIMEOUT,
)
from app.modules.user import projects, teams
from app.modules.user import tenants as tenants_ctx

logger = logging.getLogger(__name__)

_TENANT_PROJECTS = "SELECT * FROM admin_tenant_projects($1)"
_TENANT_MEMBERS = "SELECT * FROM admin_tenant_members($1)"
_TENANT_DATASETS = "SELECT * FROM admin_tenant_datasets($1)"


class TenantPage(BaseModel):
    rows: list[dict]
    total: int  # the size of the filtered set
    status_counts: dict[str, int]  # the filtered set by status, before the status filter


def _no_such_tenant() -> HTTPException:
    return HTTPException(status_code=404, detail=NO_SUCH_TENANT)


def _zitadel_refused(error: httpx.HTTPStatusError) -> HTTPException:
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


def _zitadel_unreachable(error: Exception) -> HTTPException:
    return HTTPException(status_code=502, detail=f"zitadel unreachable: {error}")


async def _owners(client: httpx.AsyncClient, tenants: list[dict]) -> list[dict | None]:
    semaphore = asyncio.Semaphore(OWNER_CONCURRENCY)

    async def one(org_id: str):
        async with semaphore:
            return await zitadel.get_org_owner(client, org_id)

    return await asyncio.gather(*(one(t["zitadel_org_id"]) for t in tenants))


async def list_page(q: str | None, plan_tier: str | None, status: str | None, limit: int, offset: int) -> TenantPage:
    """One page of every tenant, newest last, filtered on name, plan and (after enrichment) Zitadel status."""
    async with (await get_pool()).acquire() as conn:
        rows = await tenants_ctx.admin_list(conn, q, plan_tier, MAX_SCAN)

    async with httpx.AsyncClient(timeout=ZITADEL_TIMEOUT) as client:
        org_states = await zitadel.list_org_states(client)
        tenants = [
            dict(row.model_dump(), status=org_states.get(row.zitadel_org_id, IdentityStatus.UNKNOWN)) for row in rows
        ]
        # Before the status filter, so a status-filtered request still knows
        # what the other tabs hold and a back-office UI doesn't have to ask
        # three times to label them.
        status_counts = collections.Counter(t["status"] for t in tenants)
        if status is not None:
            tenants = [t for t in tenants if t["status"] == status]

        page = tenants[offset : offset + limit]
        for tenant, owner in zip(page, await _owners(client, page)):
            tenant["owner"] = owner
    return TenantPage(rows=page, total=len(tenants), status_counts=status_counts)


async def create_tenant(name: str, plan_tier: str, zitadel_org_id: str | None) -> dict:
    """The Zitadel org (unless one is adopted), the tenant row that maps to it, and its Default team.

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
    if plan_tier not in PlanTier:
        raise HTTPException(status_code=400, detail=f"plan_tier must be one of {tuple(t.value for t in PlanTier)}")

    async with httpx.AsyncClient(timeout=ZITADEL_TIMEOUT) as client:
        adopted = zitadel_org_id is not None
        if not adopted:
            try:
                zitadel_org_id = await zitadel.create_org(client, name)
            except httpx.HTTPStatusError as error:
                raise _zitadel_refused(error) from error
            except (httpx.HTTPError, OSError) as error:
                raise _zitadel_unreachable(error) from error

        try:
            tenant = await _insert_tenant(zitadel_org_id, name, plan_tier)
        except Exception:
            if not adopted:
                await _discard_org(client, zitadel_org_id)
            raise

    # A freshly created org is active, and an adopted one is whatever
    # Zitadel already says it is — but that is one more round trip for a
    # value the caller is about to read from the listing anyway.
    return dict(tenant, status=IdentityStatus.UNKNOWN if adopted else IdentityStatus.ACTIVE)


async def _insert_tenant(zitadel_org_id: str, name: str, plan_tier: str) -> dict:
    async with (await get_pool()).acquire() as conn:
        try:
            row = await tenants_ctx.insert_admin_tenant(conn, zitadel_org_id, name, plan_tier)
        except asyncpg.UniqueViolationError as error:
            raise HTTPException(
                status_code=409,
                detail=f"org {zitadel_org_id} is already mapped to a tenant",
            ) from error

    # teams is RLS-protected, so the Default team is written as the tenant
    # rather than around it — the same connection its own members get.
    async with TenantConn(str(row.id)) as conn:
        await teams.create_team(conn, row.id, teams.DEFAULT_TEAM_NAME)
    return row.model_dump()


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


async def summary() -> dict:
    """Platform-wide totals, counted in Postgres (admin_platform_stats()) rather than over
    the listing's rows, so the numbers don't stop at MAX_SCAN or at one page.

    `status_counts` degrades like the listing: with Zitadel unreachable every
    organisation counts as unknown, which is honest about not knowing rather
    than reporting zero suspended.
    """
    async with (await get_pool()).acquire() as conn:
        stats = await tenants_ctx.admin_platform_stats(conn)
        org_ids = await tenants_ctx.admin_org_ids(conn)

    async with httpx.AsyncClient(timeout=ZITADEL_TIMEOUT) as client:
        org_states = await zitadel.list_org_states(client)
    # Keyed off the tenants table, not off the Zitadel search: an org on the
    # instance with no tenant row (Zitadel's own instance org, for one) is
    # not an organisation this platform has.
    counts = collections.Counter(org_states.get(org_id, IdentityStatus.UNKNOWN) for org_id in org_ids)

    return {
        "organizations": stats["tenant_count"],
        "organizations_new_30d": stats["tenant_count_30d"],
        "projects": stats["project_count"],
        "projects_new_30d": stats["project_count_30d"],
        "members": stats["member_count"],
        "status_counts": {status: counts.get(status, 0) for status in ORG_STATUSES},
    }


async def get_tenant(tenant_id: uuid.UUID) -> dict:
    """Identity, owner, status and counts of one tenant."""
    async with (await get_pool()).acquire() as conn:
        row = await tenants_ctx.admin_detail(conn, tenant_id)
    if row is None:
        raise _no_such_tenant()

    async with httpx.AsyncClient(timeout=ZITADEL_TIMEOUT) as client:
        # The instance-wide search is one call and already handles being
        # unreachable, which a per-org read would have to repeat.
        states = await zitadel.list_org_states(client)
        owner = await zitadel.get_org_owner(client, row.zitadel_org_id)

    return dict(row.model_dump(), status=states.get(row.zitadel_org_id, IdentityStatus.UNKNOWN), owner=owner)


async def list_projects(tenant_id: uuid.UUID) -> list[dict]:
    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch(_TENANT_PROJECTS, tenant_id)
    return [dict(row) for row in rows]


async def list_members(tenant_id: uuid.UUID) -> list[dict]:
    """project_members holds the membership and Zitadel holds the person, so each row is one joined onto the other."""
    async with (await get_pool()).acquire() as conn:
        org_ids = await tenants_ctx.get_org_ids(conn, [tenant_id])
        if tenant_id not in org_ids:
            raise _no_such_tenant()
        org_id = org_ids[tenant_id]  # None for a tenant created through POST /v1/tenants
        rows = await conn.fetch(_TENANT_MEMBERS, tenant_id)

    async with httpx.AsyncClient(timeout=ZITADEL_TIMEOUT) as client:
        users = await zitadel.list_org_users(client, org_id)
    return [dict(row, **(users.get(row["user_id"]) or UNKNOWN_USER)) for row in rows]


async def list_datasets(tenant_id: uuid.UUID) -> list[dict]:
    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch(_TENANT_DATASETS, tenant_id)
    return [dict(row) for row in rows]


async def create_project(tenant_id: uuid.UUID, name: str, task_type: TaskType, actor: str) -> dict:
    """A project inside the tenant, as the tenant, under the same rules as the user-facing create."""
    async with (await get_pool()).acquire() as conn:
        await task_types.require_supported(conn, task_type)
        if await tenants_ctx.get_ref(conn, tenant_id) is None:
            raise _no_such_tenant()

    async with TenantConn(str(tenant_id)) as conn:
        team_id = await teams.get_default_team_id(conn)
        if team_id is None:
            raise HTTPException(status_code=409, detail="tenant has no team; create one first")
        created = await projects.create_project(conn, tenant_id, name, team_id, task_type, actor)
    return created.model_dump(exclude={"team_id"})


async def update_settings(tenant_id: uuid.UUID, settings: TenantSettings) -> dict:
    """A merge, not a replace: two operators on two switches don't overwrite each other."""
    async with (await get_pool()).acquire() as conn:
        stored = await tenants_ctx.merge_settings(conn, tenant_id, settings.model_dump(exclude_unset=True))
    if stored is None:
        raise _no_such_tenant()
    return {"settings": TenantSettings.model_validate(stored).model_dump()}


async def set_status(tenant_id: uuid.UUID, status: str) -> dict:
    """tenants.status is what require() enforces. A tenant with a Zitadel org has that org moved
    too; the UPDATE rolls back if Zitadel refuses, so the two never disagree."""
    if status not in ADMIN_SETTABLE_STATUSES:
        raise HTTPException(status_code=400, detail="status must be 'active' or 'suspended'")

    async with (await get_pool()).acquire() as conn, conn.transaction():
        org_id = await tenants_ctx.set_status(conn, tenant_id, status)
        if org_id is None:
            raise _no_such_tenant()
        if org_id:
            async with httpx.AsyncClient(timeout=ZITADEL_TIMEOUT) as client:
                try:
                    await zitadel.set_org_state(client, org_id, active=status == TenantStatus.ACTIVE)
                except httpx.HTTPStatusError as error:
                    # The org is gone, or is already in that state: Zitadel's own
                    # wording, same as provisioning passes through.
                    raise _zitadel_refused(error) from error
                except (httpx.HTTPError, OSError) as error:
                    raise _zitadel_unreachable(error) from error

    return {"status": status}
