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
from http import HTTPStatus

import asyncpg
import httpx

from app.core import task_types
from app.core.authz import TenantRole, TenantStatus
from app.core.authz.deps import TenantProblem
from app.core.db import get_pool
from app.core.enums import IdentityStatus, PlanTier, TaskType
from app.core.problem import Problem, not_found
from app.core.tenant_conn import TenantConn
from app.core.tenant_settings import TenantSettings
from app.integrations import zitadel
from app.integrations.zitadel import OrgOwner
from app.modules.admin.tenants.constants import ADMIN_SETTABLE_STATUSES, MAX_SCAN, ORG_STATUSES, ProblemCode
from app.modules.admin.tenants.schemas import (
    CreatedTenant,
    TenantDataset,
    TenantDetail,
    TenantListItem,
    TenantMember,
    TenantPage,
    TenantProject,
    TenantSettingsOut,
    TenantsSummary,
    TenantStatusOut,
)
from app.modules.user import projects, teams
from app.modules.user import tenants as tenants_ctx
from app.modules.user.projects import CreatedProject
from app.modules.user.tenant_members import add_member

logger = logging.getLogger(__name__)

_TENANT_PROJECTS = "SELECT * FROM admin_tenant_projects($1)"
_TENANT_MEMBERS = "SELECT * FROM admin_tenant_members($1)"
_TENANT_DATASETS = "SELECT * FROM admin_tenant_datasets($1)"


def _zitadel_refused(error: httpx.HTTPStatusError) -> Problem:
    """Zitadel's own refusal, at the status the caller should see.

    Its message says what happened — the org name is taken, the org is
    already deactivated, it doesn't exist — better than anything this
    layer could infer from a status code.
    """
    try:
        detail = error.response.json().get("message", error.response.text)
    except ValueError:
        detail = error.response.text
    status = HTTPStatus.CONFLICT if error.response.status_code < HTTPStatus.INTERNAL_SERVER_ERROR else HTTPStatus.BAD_GATEWAY
    return Problem(status, ProblemCode.ZITADEL_REFUSED, detail)


def _zitadel_unreachable(error: Exception) -> Problem:
    return Problem(HTTPStatus.BAD_GATEWAY, ProblemCode.ZITADEL_UNREACHABLE, f"zitadel unreachable: {error}")


async def _owners(client: httpx.AsyncClient, org_ids: list[str | None]) -> list[OrgOwner | None]:
    semaphore = asyncio.Semaphore(zitadel.FANOUT_CONCURRENCY)

    async def one(org_id: str | None):
        async with semaphore:
            return await zitadel.get_org_owner(client, org_id)

    return await asyncio.gather(*(one(org_id) for org_id in org_ids))


async def list_page(q: str | None, plan_tier: str | None, status: str | None, limit: int, offset: int) -> TenantPage:
    """One page of every tenant, newest last, filtered on name, plan and (after enrichment) Zitadel status."""
    async with (await get_pool()).acquire() as conn:
        rows = await tenants_ctx.admin_list(conn, q, plan_tier, MAX_SCAN)

    async with zitadel.client() as client:
        org_states = await zitadel.list_org_states(client)
        tenants = [(row, org_states.get(row.zitadel_org_id, IdentityStatus.UNKNOWN)) for row in rows]
        # Before the status filter, so a status-filtered request still knows
        # what the other tabs hold and a back-office UI doesn't have to ask
        # three times to label them.
        status_counts = collections.Counter(s for _, s in tenants)
        if status is not None:
            tenants = [(row, s) for row, s in tenants if s == status]

        page = tenants[offset : offset + limit]
        owners = await _owners(client, [row.zitadel_org_id for row, _ in page])
    items = [TenantListItem(**row.model_dump(), status=s, owner=o) for (row, s), o in zip(page, owners)]
    return TenantPage(rows=items, total=len(tenants), status_counts=status_counts)


async def create_tenant(name: str, plan_tier: str, zitadel_org_id: str | None, actor: str) -> CreatedTenant:
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
        raise Problem(HTTPStatus.BAD_REQUEST, ProblemCode.NAME_REQUIRED, "name is required")
    if plan_tier not in PlanTier:
        tiers = tuple(t.value for t in PlanTier)
        raise Problem(HTTPStatus.BAD_REQUEST, ProblemCode.INVALID_PLAN_TIER, f"plan_tier must be one of {tiers}")

    async with zitadel.client() as client:
        adopted = zitadel_org_id is not None
        if not adopted:
            try:
                zitadel_org_id = await zitadel.create_org(client, name)
            except httpx.HTTPStatusError as error:
                raise _zitadel_refused(error) from error
            except (httpx.HTTPError, OSError) as error:
                raise _zitadel_unreachable(error) from error

        try:
            tenant = await _insert_tenant(zitadel_org_id, name, plan_tier, actor)
        except Exception:
            if not adopted:
                await _discard_org(client, zitadel_org_id)
            raise

    # A freshly created org is active, and an adopted one is whatever
    # Zitadel already says it is — but that is one more round trip for a
    # value the caller is about to read from the listing anyway.
    return CreatedTenant(**tenant.model_dump(), status=IdentityStatus.UNKNOWN if adopted else IdentityStatus.ACTIVE)


async def _insert_tenant(
    zitadel_org_id: str, name: str, plan_tier: str, actor: str
) -> tenants_ctx.AdminTenantCreated:
    async with (await get_pool()).acquire() as conn:
        try:
            row = await tenants_ctx.insert_admin_tenant(conn, zitadel_org_id, name, plan_tier)
        except asyncpg.UniqueViolationError as error:
            raise Problem(
                HTTPStatus.CONFLICT, ProblemCode.ORG_ALREADY_MAPPED, f"org {zitadel_org_id} is already mapped to a tenant"
            ) from error

    # teams is RLS-protected, so the Default team is written as the tenant
    # rather than around it — the same connection its own members get.
    async with TenantConn(str(row.id)) as conn:
        await teams.create_team(conn, row.id, teams.DEFAULT_TEAM_NAME)
        if plan_tier == PlanTier.INTERNAL:
            await add_member(conn, row.id, actor, TenantRole.OWNER)
    return row


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


async def summary() -> TenantsSummary:
    """Platform-wide totals, counted in Postgres (admin_platform_stats()) rather than over
    the listing's rows, so the numbers don't stop at MAX_SCAN or at one page.

    `status_counts` degrades like the listing: with Zitadel unreachable every
    organisation counts as unknown, which is honest about not knowing rather
    than reporting zero suspended.
    """
    async with (await get_pool()).acquire() as conn:
        stats = await tenants_ctx.admin_platform_stats(conn)
        org_ids = await tenants_ctx.admin_org_ids(conn)

    async with zitadel.client() as client:
        org_states = await zitadel.list_org_states(client)
    # Keyed off the tenants table, not off the Zitadel search: an org on the
    # instance with no tenant row (Zitadel's own instance org, for one) is
    # not an organisation this platform has.
    counts = collections.Counter(org_states.get(org_id, IdentityStatus.UNKNOWN) for org_id in org_ids)

    return TenantsSummary(
        organizations=stats["tenant_count"],
        organizations_new_30d=stats["tenant_count_30d"],
        projects=stats["project_count"],
        projects_new_30d=stats["project_count_30d"],
        members=stats["member_count"],
        status_counts={status: counts.get(status, 0) for status in ORG_STATUSES},
    )


async def get_tenant(tenant_id: uuid.UUID) -> TenantDetail:
    """Identity, owner, status and counts of one tenant."""
    async with (await get_pool()).acquire() as conn:
        row = await tenants_ctx.admin_detail(conn, tenant_id)
    if row is None:
        raise not_found()

    async with zitadel.client() as client:
        # The instance-wide search is one call and already handles being
        # unreachable, which a per-org read would have to repeat.
        states = await zitadel.list_org_states(client)
        owner = await zitadel.get_org_owner(client, row.zitadel_org_id)

    return TenantDetail(**row.model_dump(), status=states.get(row.zitadel_org_id, IdentityStatus.UNKNOWN), owner=owner)


async def list_projects(tenant_id: uuid.UUID) -> list[TenantProject]:
    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch(_TENANT_PROJECTS, tenant_id)
    return [TenantProject.model_validate(dict(row)) for row in rows]


async def list_members(tenant_id: uuid.UUID) -> list[TenantMember]:
    """project_members holds the membership and Zitadel holds the person, so each row is one joined onto the other.
    A member Zitadel does not answer for keeps the row, as UNKNOWN_USER: the membership is still real."""
    async with (await get_pool()).acquire() as conn:
        org_ids = await tenants_ctx.get_org_ids(conn, [tenant_id])
        if tenant_id not in org_ids:
            raise not_found()
        org_id = org_ids[tenant_id]  # None for a tenant created through POST /v1/tenants
        rows = await conn.fetch(_TENANT_MEMBERS, tenant_id)

    async with zitadel.client() as client:
        users = await zitadel.list_org_users(client, org_id)
    return [
        TenantMember(**dict(row), **users.get(row["user_id"], zitadel.UNKNOWN_USER).model_dump()) for row in rows
    ]


async def list_datasets(tenant_id: uuid.UUID) -> list[TenantDataset]:
    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch(_TENANT_DATASETS, tenant_id)
    return [TenantDataset.model_validate(dict(row)) for row in rows]


async def create_project(tenant_id: uuid.UUID, name: str, task_type: TaskType, actor: str) -> CreatedProject:
    """A project inside the tenant, as the tenant, under the same rules as the user-facing create."""
    async with (await get_pool()).acquire() as conn:
        await task_types.require_supported(conn, task_type)
        if await tenants_ctx.get_ref(conn, tenant_id) is None:
            raise not_found()

    async with TenantConn(str(tenant_id)) as conn:
        team_id = await teams.get_default_team_id(conn)
        if team_id is None:
            raise Problem(HTTPStatus.CONFLICT, ProblemCode.NO_TEAM, "tenant has no team; create one first")
        return await projects.create_project(conn, tenant_id, name, team_id, task_type, actor)


async def update_settings(tenant_id: uuid.UUID, settings: TenantSettings) -> TenantSettingsOut:
    """A merge, not a replace: two operators on two switches don't overwrite each other."""
    async with (await get_pool()).acquire() as conn:
        stored = await tenants_ctx.merge_settings(conn, tenant_id, settings.model_dump(exclude_unset=True))
    if stored is None:
        raise not_found()
    return TenantSettingsOut(settings=TenantSettings.model_validate(stored))


async def set_status(tenant_id: uuid.UUID, status: str) -> TenantStatusOut:
    """tenants.status is what require() enforces. A tenant with a Zitadel org has that org moved
    too; the UPDATE rolls back if Zitadel refuses, so the two never disagree."""
    if status not in ADMIN_SETTABLE_STATUSES:
        raise Problem(HTTPStatus.BAD_REQUEST, ProblemCode.STATUS_NOT_SETTABLE, "status must be 'active' or 'suspended'")

    async with (await get_pool()).acquire() as conn, conn.transaction():
        changed = await tenants_ctx.set_status(conn, tenant_id, status)
        if changed is None:
            raise not_found()
        if changed.previous == TenantStatus.PENDING_DELETE:
            raise Problem(HTTPStatus.CONFLICT, TenantProblem.PENDING_DELETE, "tenant is pending deletion; only its owner can restore it")
        org_id = changed.org_id
        if org_id:
            async with zitadel.client() as client:
                try:
                    await zitadel.set_org_state(client, org_id, active=status == TenantStatus.ACTIVE)
                except httpx.HTTPStatusError as error:
                    # The org is gone, or is already in that state: Zitadel's own
                    # wording, same as provisioning passes through.
                    raise _zitadel_refused(error) from error
                except (httpx.HTTPError, OSError) as error:
                    raise _zitadel_unreachable(error) from error

    return TenantStatusOut(status=status)
