"""Tenant self-service HTTP surface. Rename and the audit log are for owner and admin; delete and restore for owners only."""
import uuid

from fastapi import APIRouter, Depends

from app.core.authz import Action, AuthzContext, require
from app.core.pagination import Page, PageParams
from app.core.tenant_settings import TenantSettings
from app.modules.user.tenants import service
from app.modules.user.tenants.schemas import TenantAuditEntry, TenantDelete, TenantDetail, TenantRename

router = APIRouter(prefix="/v1/tenants", tags=["tenants"])


@router.get("/{tenant_id}", response_model=TenantDetail)
async def get_tenant(tenant_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.PROJECT_LIST))):
    ctx.ensure_tenant(tenant_id)
    return await service.get(ctx)


@router.patch("/{tenant_id}", response_model=TenantDetail)
async def rename_tenant(tenant_id: uuid.UUID, body: TenantRename, ctx: AuthzContext = Depends(require(Action.TENANT_ADMIN))):
    ctx.ensure_tenant(tenant_id)
    return await service.rename(ctx, body.name)


@router.get("/{tenant_id}/settings", response_model=TenantSettings)
async def get_settings(tenant_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.PROJECT_LIST))):
    ctx.ensure_tenant(tenant_id)
    return await service.get_settings(ctx)


@router.patch("/{tenant_id}/settings", response_model=TenantSettings)
async def update_settings(tenant_id: uuid.UUID, body: TenantSettings, ctx: AuthzContext = Depends(require(Action.TENANT_ADMIN))):
    ctx.ensure_tenant(tenant_id)
    return await service.update_settings(ctx, body)


@router.post("/{tenant_id}:delete", response_model=TenantDetail)
async def delete_tenant(tenant_id: uuid.UUID, body: TenantDelete, ctx: AuthzContext = Depends(require(Action.TENANT_DELETE))):
    ctx.ensure_tenant(tenant_id)
    return await service.delete(ctx, body.confirm_name)


@router.post("/{tenant_id}:restore", response_model=TenantDetail)
async def restore_tenant(
    tenant_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.TENANT_DELETE, allow_pending_delete=True))
):
    ctx.ensure_tenant(tenant_id)
    return await service.restore(ctx)


@router.get("/{tenant_id}/audit-log", response_model=Page[TenantAuditEntry])
async def get_audit_log(
    tenant_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.TENANT_AUDIT_READ)), page: PageParams = Depends()
):
    ctx.ensure_tenant(tenant_id)
    return await service.audit_log(ctx, page)
