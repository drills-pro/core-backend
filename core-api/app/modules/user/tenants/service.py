from app.core import audit
from app.core.authz import AuthzContext
from app.core.pagination import Page, PageParams, SortDir, build_page, keyset_clause, order_by
from app.core.problem import Problem
from app.core.tenant_conn import TenantConn
from app.core.tenant_settings import TenantSettings
from app.modules.user.tenants import transitions
from app.modules.user.tenants.constants import AuditAction, EntityType, ProblemCode
from app.modules.user.tenants.schemas import TenantAuditEntry, TenantDetail

_DETAIL = "SELECT id, name, slug, status, plan_tier, created_at, deleted_at FROM tenants WHERE id = $1"


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


async def get_settings(ctx: AuthzContext) -> TenantSettings:
    async with TenantConn(ctx.tenant_id) as conn:
        return TenantSettings.model_validate(await conn.fetchval("SELECT settings FROM tenants WHERE id = $1", ctx.tenant_id))


async def update_settings(ctx: AuthzContext, changes: TenantSettings) -> TenantSettings:
    """Merge only the fields the caller sent, so two people on two switches do not overwrite each other."""
    async with TenantConn(ctx.tenant_id) as conn:
        stored = await conn.fetchval(
            "UPDATE tenants SET settings = settings || $2::jsonb WHERE id = $1 RETURNING settings",
            ctx.tenant_id, changes.model_dump(exclude_unset=True),
        )
        await _record(conn, ctx, AuditAction.UPDATE_SETTINGS)
        return TenantSettings.model_validate(stored)


async def delete(ctx: AuthzContext, confirm_name: str) -> TenantDetail:
    """Soft delete: the tenant is blocked at once and restorable until the purge (planning/03: 30 days)."""
    async with TenantConn(ctx.tenant_id) as conn:
        current = await conn.fetchval("SELECT name FROM tenants WHERE id = $1", ctx.tenant_id)
        if confirm_name != current:
            raise Problem(400, ProblemCode.CONFIRMATION_MISMATCH, "confirm_name must equal the tenant name")
        await transitions.mark_pending_delete(conn, ctx.tenant_id)
        await _record(conn, ctx, AuditAction.DELETE)
        return _detail(await conn.fetchrow(_DETAIL, ctx.tenant_id), ctx)


async def restore(ctx: AuthzContext) -> TenantDetail:
    async with TenantConn(ctx.tenant_id) as conn:
        await transitions.restore(conn, ctx.tenant_id)
        await _record(conn, ctx, AuditAction.RESTORE)
        return _detail(await conn.fetchrow(_DETAIL, ctx.tenant_id), ctx)


async def audit_log(ctx: AuthzContext, page: PageParams) -> Page[TenantAuditEntry]:
    """Every audit row of the tenant, project events included, newest first. RLS scopes the table to the tenant."""
    async with TenantConn(ctx.tenant_id) as conn:
        cond, args = keyset_clause(page.page_token, SortDir.DESC, first_param=1)
        rows = await conn.fetch(
            "SELECT id, project_id, actor, action, entity_type, entity_id, created_at FROM audit_log "
            f"WHERE true {cond} ORDER BY {order_by(SortDir.DESC)} LIMIT {page.page_size + 1}",
            *args,
        )
    return build_page(rows, page.page_size, TenantAuditEntry)
