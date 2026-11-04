"""One tenant, as the operator opens it from the listing in routes.py.

Same three sources as the listing (tenants, SECURITY DEFINER counters,
Zitadel), asked about a single tenant instead of all of them, plus the
three writes the back-office owns:

    POST   /{id}/status     suspend or reactivate  -> tenants.status (+ Zitadel org)
    PATCH  /{id}/settings   org policy switches    -> tenants.settings
    POST   /{id}/projects   create a project       -> that tenant's RLS

The org-level policy switches are stored (tenants.settings) and returned, but nothing in
the user-facing API reads them yet: they record the operator's intent,
and the modules that would enforce them — project creation, training
submission, publishing — still have to be taught to ask.
"""
import uuid

from fastapi import APIRouter, Body, Depends

from app.core.auth import get_platform_admin
from app.core.enums import TaskType
from app.core.tenant_settings import TenantSettings
from app.modules.admin.tenants import service

router = APIRouter(prefix="/v1/admin/tenants", tags=["admin"])


@router.get("/{tenant_id}", dependencies=[Depends(get_platform_admin)])
async def get_tenant(tenant_id: uuid.UUID):
    """The header of the detail page: identity, owner, status, counts."""
    return await service.get_tenant(tenant_id)


@router.get("/{tenant_id}/projects", dependencies=[Depends(get_platform_admin)])
async def list_tenant_projects(tenant_id: uuid.UUID):
    """Every live project in the tenant, newest first, with what the card
    shows: its datasets, and the newest model registered against it."""
    return await service.list_projects(tenant_id)


@router.get("/{tenant_id}/members", dependencies=[Depends(get_platform_admin)])
async def list_tenant_members(tenant_id: uuid.UUID):
    """One row per person with access to a project in this tenant.

    A member of the Zitadel org who is on no project yet is not here — same
    definition as the member count in the listing (admin_tenant_stats()), so
    the two agree.
    """
    return await service.list_members(tenant_id)


@router.get("/{tenant_id}/datasets", dependencies=[Depends(get_platform_admin)])
async def list_tenant_datasets(tenant_id: uuid.UUID):
    return await service.list_datasets(tenant_id)


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
    return await service.create_project(tenant_id, name, task_type, actor)


@router.patch("/{tenant_id}/settings", dependencies=[Depends(get_platform_admin)])
async def update_tenant_settings(tenant_id: uuid.UUID, settings: TenantSettings):
    """Merge a partial settings object into the tenant's, and return all of it.

    The page sends the one switch that moved."""
    return await service.update_settings(tenant_id, settings)


@router.post("/{tenant_id}/status", dependencies=[Depends(get_platform_admin)])
async def set_tenant_status(tenant_id: uuid.UUID, status: str = Body(..., embed=True)):
    """Suspend or reactivate the tenant."""
    return await service.set_status(tenant_id, status)
