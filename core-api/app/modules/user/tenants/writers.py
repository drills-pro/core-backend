"""What other contexts may write to tenants, on the caller's connection and transaction."""
import uuid

from app.modules.user.tenants.schemas import AdminTenantCreated

_INSERT = "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3) RETURNING slug"


async def insert_tenant(conn, tenant_id: uuid.UUID, name: str, slug: str | None) -> str:
    """Insert the tenant; a None slug lets the tenants_set_slug trigger pick one. Returns the stored slug."""
    return await conn.fetchval(_INSERT, tenant_id, name, slug)

_ADMIN_INSERT = (
    "INSERT INTO tenants (zitadel_org_id, name, plan_tier) VALUES ($1, $2, $3) "
    "RETURNING id, name, zitadel_org_id, plan_tier, created_at"
)
_UPDATE_SETTINGS = "UPDATE tenants SET settings = settings || $2::jsonb WHERE id = $1 RETURNING settings"
_SET_STATUS = "UPDATE tenants SET status = $2 WHERE id = $1 RETURNING coalesce(zitadel_org_id, '')"


async def insert_admin_tenant(conn, zitadel_org_id: str, name: str, plan_tier: str) -> AdminTenantCreated:
    """Raises asyncpg.UniqueViolationError when the org is already mapped to a tenant."""
    return AdminTenantCreated.model_validate(dict(await conn.fetchrow(_ADMIN_INSERT, zitadel_org_id, name, plan_tier)))


async def merge_settings(conn, tenant_id: uuid.UUID, patch: dict) -> dict | None:
    """Merge `patch` into tenants.settings; the stored settings, or None when there is no such tenant."""
    row = await conn.fetchrow(_UPDATE_SETTINGS, tenant_id, patch)
    return None if row is None else row["settings"]


async def set_status(conn, tenant_id: uuid.UUID, status: str) -> str | None:
    """Set tenants.status; the Zitadel org id ('' when none), or None when there is no such tenant."""
    return await conn.fetchval(_SET_STATUS, tenant_id, status)
