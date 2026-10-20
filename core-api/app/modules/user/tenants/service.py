from app.core import audit
from app.core.authz import AuthzContext, TenantStatus
from app.core.problem import Problem
from app.core.tenant_conn import TenantConn
from app.modules.user.tenants.constants import AuditAction, EntityType, ProblemCode
from app.modules.user.tenants.schemas import TenantDetail

_DETAIL = "SELECT id, name, status, plan_tier, created_at, deleted_at FROM tenants WHERE id = $1"


async def _record(conn, ctx: AuthzContext, action: AuditAction) -> None:
    await audit.record(conn, tenant_id=ctx.tenant_id, project_id=None, actor=ctx.actor,
                       action=action, entity_type=EntityType.TENANT, entity_id=ctx.tenant_id)


def _detail(row, ctx: AuthzContext) -> TenantDetail:
    return TenantDetail.model_validate({**dict(row), "role": ctx.tenant_role})


async def get(ctx: AuthzContext) -> TenantDetail:
    async with TenantConn(ctx.tenant_id) as conn:
        return _detail(await conn.fetchrow(_DETAIL, ctx.tenant_id), ctx)


async def rename(ctx: AuthzContext, name: str) -> TenantDetail:
    async with TenantConn(ctx.tenant_id) as conn:
        await conn.execute("UPDATE tenants SET name = $2 WHERE id = $1", ctx.tenant_id, name)
        await _record(conn, ctx, AuditAction.RENAME)
        return _detail(await conn.fetchrow(_DETAIL, ctx.tenant_id), ctx)


async def delete(ctx: AuthzContext, confirm_name: str) -> TenantDetail:
    """Soft delete: the tenant is blocked at once and restorable until the purge (planning/03: 30 days)."""
    async with TenantConn(ctx.tenant_id) as conn:
        current = await conn.fetchval("SELECT name FROM tenants WHERE id = $1", ctx.tenant_id)
        if confirm_name != current:
            raise Problem(400, ProblemCode.CONFIRMATION_MISMATCH, "confirm_name must equal the tenant name")
        await conn.execute("UPDATE tenants SET status = $2, deleted_at = now() WHERE id = $1", ctx.tenant_id, TenantStatus.PENDING_DELETE)
        await _record(conn, ctx, AuditAction.DELETE)
        return _detail(await conn.fetchrow(_DETAIL, ctx.tenant_id), ctx)


async def restore(ctx: AuthzContext) -> TenantDetail:
    async with TenantConn(ctx.tenant_id) as conn:
        restored = await conn.fetchval(
            "UPDATE tenants SET status = $2, deleted_at = NULL WHERE id = $1 AND status = $3 RETURNING id",
            ctx.tenant_id, TenantStatus.ACTIVE, TenantStatus.PENDING_DELETE,
        )
        if restored is None:
            raise Problem(409, ProblemCode.NOT_PENDING_DELETE, "this tenant is not pending deletion")
        await _record(conn, ctx, AuditAction.RESTORE)
        return _detail(await conn.fetchrow(_DETAIL, ctx.tenant_id), ctx)
