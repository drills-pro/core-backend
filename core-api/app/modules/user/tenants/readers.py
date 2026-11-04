"""What other contexts may ask of tenants. The reader takes the caller's connection, so it is one more query in
the caller's transaction, under the same RLS."""
import uuid

from app.core.enums import PlanTier
from app.modules.user.tenants.schemas import AdminTenantFull, AdminTenantRow, TenantRef

_PLAN_TIER = "SELECT plan_tier FROM tenants WHERE id = $1"
_REF = "SELECT id, name, slug FROM tenants WHERE id = $1"
_SLUG_TAKEN = "SELECT true FROM tenants WHERE slug = $1"
_ORG_IDS = "SELECT id, zitadel_org_id FROM tenants WHERE id = ANY($1::uuid[])"
_ADMIN_LIST = (
    "SELECT t.id, t.name, t.zitadel_org_id, t.plan_tier, t.created_at, "
    "       s.project_count, s.member_count "
    "FROM tenants t "
    # LATERAL so the counters run for the rows this filter keeps,
    # not for every tenant on the platform.
    "JOIN LATERAL admin_tenant_stats(ARRAY[t.id]) s ON true "
    "WHERE ($1::text IS NULL OR t.name ILIKE '%' || $1 || '%') "
    # Internal (staff test) tenants show only when that tier is asked for by name.
    "AND (t.plan_tier = $2 OR ($2::text IS NULL AND t.plan_tier <> $4)) "
    "ORDER BY t.created_at, t.id LIMIT $3"
)
_ADMIN_ALL_ORG_IDS = "SELECT zitadel_org_id FROM tenants WHERE plan_tier <> $1"
_ADMIN_PLATFORM_STATS = "SELECT * FROM admin_platform_stats()"
_ADMIN_DETAIL = (
    "SELECT t.id, t.name, t.zitadel_org_id, t.plan_tier, t.created_at, t.settings, "
    "       s.project_count, s.member_count, s.dataset_count, s.model_count "
    "FROM tenants t "
    "JOIN LATERAL admin_tenant_detail_stats(t.id) s ON true "
    "WHERE t.id = $1"
)


async def get_plan_tier(conn, tenant_id: uuid.UUID) -> PlanTier | None:
    """The tenant's plan tier; None when the tenant is not visible to the caller."""
    value = await conn.fetchval(_PLAN_TIER, tenant_id)
    return None if value is None else PlanTier(value)


async def get_ref(conn, tenant_id: uuid.UUID) -> TenantRef | None:
    row = await conn.fetchrow(_REF, tenant_id)
    return None if row is None else TenantRef.model_validate(dict(row))


async def slug_taken(conn, slug: str) -> bool:
    return bool(await conn.fetchval(_SLUG_TAKEN, slug))


async def get_org_ids(conn, tenant_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """{tenant_id: zitadel_org_id}. tenants has no RLS (init migration), so this is a plain SELECT."""
    rows = await conn.fetch(_ORG_IDS, tenant_ids)
    return {row["id"]: row["zitadel_org_id"] for row in rows}


async def admin_list(conn, q: str | None, plan_tier: str | None, limit: int) -> list[AdminTenantRow]:
    """Back-office listing rows, oldest first, with the project and member counters. No plan_tier leaves internal
    tenants out."""
    rows = await conn.fetch(_ADMIN_LIST, q, plan_tier, limit, PlanTier.INTERNAL)
    return [AdminTenantRow.model_validate(dict(row)) for row in rows]


async def admin_org_ids(conn) -> list[str | None]:
    """The zitadel_org_id of every customer tenant, internal ones left out (None for one created without an org)."""
    return [row["zitadel_org_id"] for row in await conn.fetch(_ADMIN_ALL_ORG_IDS, PlanTier.INTERNAL)]


async def admin_platform_stats(conn) -> dict:
    """The admin_platform_stats() totals row."""
    return dict(await conn.fetchrow(_ADMIN_PLATFORM_STATS))


async def admin_detail(conn, tenant_id: uuid.UUID) -> AdminTenantFull | None:
    row = await conn.fetchrow(_ADMIN_DETAIL, tenant_id)
    return None if row is None else AdminTenantFull.model_validate(dict(row))
