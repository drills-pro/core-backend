"""What other contexts may write to tenants, on the caller's connection and transaction."""
import uuid

from app.core.authz import TenantStatus
from app.modules.user.tenants.schemas import AdminTenantCreated, TenantStatusChange

_INSERT = "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3) RETURNING slug"


async def insert_tenant(conn, tenant_id: uuid.UUID, name: str, slug: str | None) -> str:
    """Insert the tenant; a None slug lets the tenants_set_slug trigger pick one. Returns the stored slug."""
    return await conn.fetchval(_INSERT, tenant_id, name, slug)

_ADMIN_INSERT = (
    "INSERT INTO tenants (zitadel_org_id, name, plan_tier) VALUES ($1, $2, $3) "
    "RETURNING id, name, zitadel_org_id, plan_tier, created_at"
)
_UPDATE_SETTINGS = "UPDATE tenants SET settings = settings || $2::jsonb WHERE id = $1 RETURNING settings"
# One statement, the row locked: the owner's :delete cannot slip in between the check and the write.
_SET_STATUS = (
    "WITH cur AS (SELECT id, status, coalesce(zitadel_org_id, '') AS org_id FROM tenants WHERE id = $1 FOR UPDATE), "
    "moved AS (UPDATE tenants t SET status = $2 FROM cur WHERE t.id = cur.id AND cur.status <> $3) "
    "SELECT status AS previous, org_id FROM cur"
)


async def insert_admin_tenant(conn, zitadel_org_id: str, name: str, plan_tier: str) -> AdminTenantCreated:
    """Raises asyncpg.UniqueViolationError when the org is already mapped to a tenant."""
    return AdminTenantCreated.model_validate(dict(await conn.fetchrow(_ADMIN_INSERT, zitadel_org_id, name, plan_tier)))


async def merge_settings(conn, tenant_id: uuid.UUID, patch: dict) -> dict | None:
    """Merge `patch` into tenants.settings; the stored settings, or None when there is no such tenant."""
    row = await conn.fetchrow(_UPDATE_SETTINGS, tenant_id, patch)
    return None if row is None else row["settings"]


async def set_status(conn, tenant_id: uuid.UUID, status: str) -> TenantStatusChange | None:
    """Set tenants.status unless the tenant is pending_delete (that is the owner's to restore, so it is left as is).
    The status it had and its Zitadel org id ('' when none), or None when there is no such tenant."""
    row = await conn.fetchrow(_SET_STATUS, tenant_id, status, TenantStatus.PENDING_DELETE)
    return None if row is None else TenantStatusChange.model_validate(dict(row))
