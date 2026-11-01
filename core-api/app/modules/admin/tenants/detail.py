"""One tenant, as the operator opens it from the listing in routes.py.

Same three sources as the listing (tenants, SECURITY DEFINER counters,
Zitadel), asked about a single tenant instead of all of them, plus the
three writes the back-office owns:

    POST   /{id}/status     suspend or reactivate  -> tenants.status (+ Zitadel org)
    PATCH  /{id}/settings   org policy switches    -> tenants.settings
    POST   /{id}/projects   create a project       -> that tenant's RLS

The write path is what separates this from the rest of /v1/admin. A
platform admin creating a project inside a tenant goes through
TenantConn, the same RLS-scoped connection a user of that tenant would
get — acting *as* the tenant rather than around it, so a mistyped id
fails the insert instead of landing a row in the wrong tenant.
"""
import uuid

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException

from app.core import audit, task_catalog
from app.core.auth import get_platform_admin
from app.core.authz import TenantStatus
from app.core.db import get_pool
from app.core.enums import TaskType
from app.core.tenant_conn import TenantConn
from app.core.tenant_settings import TenantSettings
from app.integrations import zitadel
from app.modules.user import projects, teams
from app.modules.admin.tenants.routes import ZITADEL_TIMEOUT, zitadel_refused

router = APIRouter(prefix="/v1/admin/tenants", tags=["admin"])

# The org-level policy switches, with the value a tenant that has never
# been edited reports. They are stored (tenants.settings) and returned, but nothing in
# the user-facing API reads them yet: they record the operator's intent,
# and the modules that would enforce them — project creation, training
# submission, publishing — still have to be taught to ask.
# pending_delete belongs to the owner's delete/restore flow, not to an operator's switch.
ADMIN_SETTABLE_STATUSES = (TenantStatus.ACTIVE, TenantStatus.SUSPENDED)

# A member whose Zitadel user is gone, or whose org Zitadel won't answer
# for. The listing degrades the same way (owner=null, status="unknown")
# rather than dropping the row: the membership is still real.
UNKNOWN_USER = {
    "display_name": None,
    "email": None,
    "login_name": None,
    "status": "unknown",
}


@router.get("/{tenant_id}", dependencies=[Depends(get_platform_admin)])
async def get_tenant(tenant_id: uuid.UUID):
    """The header of the detail page: identity, owner, status, counts."""
    async with (await get_pool()).acquire() as conn:
        row = await conn.fetchrow(
            "SELECT t.id, t.name, t.zitadel_org_id, t.plan_tier, t.created_at, t.settings, "
            "       s.project_count, s.member_count, s.dataset_count, s.model_count "
            "FROM tenants t "
            "JOIN LATERAL admin_tenant_detail_stats(t.id) s ON true "
            "WHERE t.id = $1",
            tenant_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="no such tenant")

    async with httpx.AsyncClient(timeout=ZITADEL_TIMEOUT) as client:
        # The instance-wide search is one call and already handles being
        # unreachable, which a per-org read would have to repeat.
        states = await zitadel.list_org_states(client)
        owner = await zitadel.get_org_owner(client, row["zitadel_org_id"])

    return dict(
        row,
        status=states.get(row["zitadel_org_id"], "unknown"),
        owner=owner,
        settings=TenantSettings.model_validate(row["settings"]).model_dump(),
    )


@router.get("/{tenant_id}/projects", dependencies=[Depends(get_platform_admin)])
async def list_tenant_projects(tenant_id: uuid.UUID):
    """Every live project in the tenant, newest first, with what the card
    shows: its datasets, and the newest model registered against it."""
    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch("SELECT * FROM admin_tenant_projects($1)", tenant_id)
    return [dict(row) for row in rows]


@router.get("/{tenant_id}/members", dependencies=[Depends(get_platform_admin)])
async def list_tenant_members(tenant_id: uuid.UUID):
    """One row per person with access to a project in this tenant.

    project_members holds the membership and Zitadel holds the person, so
    each row is one joined onto the other. A member of the Zitadel org who
    is on no project yet is not here — same definition as the member count
    in the listing (admin_tenant_stats()), so the two agree.
    """
    async with (await get_pool()).acquire() as conn:
        tenant = await conn.fetchrow("SELECT zitadel_org_id FROM tenants WHERE id = $1", tenant_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail="no such tenant")
        org_id = tenant["zitadel_org_id"]  # None for a tenant created through POST /v1/tenants
        rows = await conn.fetch("SELECT * FROM admin_tenant_members($1)", tenant_id)

    async with httpx.AsyncClient(timeout=ZITADEL_TIMEOUT) as client:
        users = await zitadel.list_org_users(client, org_id)
    return [dict(row, **(users.get(row["user_id"]) or UNKNOWN_USER)) for row in rows]


@router.get("/{tenant_id}/datasets", dependencies=[Depends(get_platform_admin)])
async def list_tenant_datasets(tenant_id: uuid.UUID):
    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch("SELECT * FROM admin_tenant_datasets($1)", tenant_id)
    return [dict(row) for row in rows]


@router.post("/{tenant_id}/projects", status_code=201)
async def create_tenant_project(
    tenant_id: uuid.UUID,
    name: str = Body(..., embed=True),
    task_type: TaskType = Body(..., embed=True),
    actor: str = Depends(get_platform_admin),
):
    """Create a project inside the tenant, as the tenant.

    Deliberately the same rules as the user-facing POST /v1/projects — the
    fixed task-type vocabulary, the tenant's oldest team as the default,
    the audit row — so a project the operator creates is indistinguishable
    from one the tenant created itself.
    """
    task_catalog.require_supported_task_type(task_type)
    async with (await get_pool()).acquire() as conn:
        if not await conn.fetchval("SELECT true FROM tenants WHERE id = $1", tenant_id):
            raise HTTPException(status_code=404, detail="no such tenant")

    async with TenantConn(str(tenant_id)) as conn:
        team_id = await teams.get_default_team_id(conn)
        if team_id is None:
            raise HTTPException(status_code=409, detail="tenant has no team; create one first")
        created = await projects.create_project(conn, tenant_id, name, team_id, task_type)
        await audit.record(
            conn,
            tenant_id=str(tenant_id),
            project_id=created.id,
            actor=actor,
            action="create",
            entity_type="project",
            entity_id=created.id,
        )
    return created.model_dump(exclude={"team_id"})


@router.patch("/{tenant_id}/settings", dependencies=[Depends(get_platform_admin)])
async def update_tenant_settings(tenant_id: uuid.UUID, settings: TenantSettings):
    """Merge a partial settings object into the tenant's, and return all of it.

    A merge, not a replace: the page sends the one switch that moved, and
    two operators on two switches don't overwrite each other.
    """
    async with (await get_pool()).acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE tenants SET settings = settings || $2::jsonb WHERE id = $1 RETURNING settings",
            tenant_id,
            settings.model_dump(exclude_unset=True),
        )
    if row is None:
        raise HTTPException(status_code=404, detail="no such tenant")
    return {"settings": TenantSettings.model_validate(row["settings"]).model_dump()}


@router.post("/{tenant_id}/status", dependencies=[Depends(get_platform_admin)])
async def set_tenant_status(tenant_id: uuid.UUID, status: str = Body(..., embed=True)):
    """Suspend or reactivate the tenant.

    tenants.status is what require() enforces. A tenant with a Zitadel org
    has that org moved too; the UPDATE rolls back if Zitadel refuses, so the
    two never disagree.
    """
    if status not in ADMIN_SETTABLE_STATUSES:
        raise HTTPException(status_code=400, detail="status must be 'active' or 'suspended'")

    async with (await get_pool()).acquire() as conn, conn.transaction():
        org_id = await conn.fetchval("UPDATE tenants SET status = $2 WHERE id = $1 RETURNING coalesce(zitadel_org_id, '')", tenant_id, status)
        if org_id is None:
            raise HTTPException(status_code=404, detail="no such tenant")
        if org_id:
            async with httpx.AsyncClient(timeout=ZITADEL_TIMEOUT) as client:
                try:
                    await zitadel.set_org_state(client, org_id, active=status == TenantStatus.ACTIVE)
                except httpx.HTTPStatusError as error:
                    # The org is gone, or is already in that state: Zitadel's own
                    # wording, same as provisioning passes through.
                    raise zitadel_refused(error) from error
                except (httpx.HTTPError, OSError) as error:
                    raise HTTPException(status_code=502, detail=f"zitadel unreachable: {error}") from error

    return {"status": status}
