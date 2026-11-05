"""Usage metering (req/09-usage-metering.md).

FR3's open question (inference compute-unit normalization formula) is
explicitly unresolved in the spec — we take the doc's own fallback: raw
GPU-seconds, no synthetic unit, pending a real pricing decision. That's
`PRICING_VERSION` below; bump it and every write after the bump carries the
new version, so past invoices stay reproducible from the version that was
active when the usage occurred.
"""
import uuid

from app.core.authz import AuthzContext
from app.core.tenant_conn import TenantConn
from app.modules.user.projects import get_team_id
from app.modules.user.usage.schemas import UsageEvent, UsageTotal

PRICING_VERSION = "v1-raw-gpu-seconds"

_PROJECT_EVENTS = (
    "SELECT id, resource_type, quantity, unit, occurred_at, source_ref, pricing_version "
    "FROM usage_events WHERE project_id = $1 ORDER BY occurred_at DESC LIMIT $2 OFFSET $3"
)
_ORG_TOTALS = (
    "SELECT project_id, resource_type, unit, SUM(quantity) AS total_quantity "
    "FROM usage_events GROUP BY project_id, resource_type, unit ORDER BY project_id, resource_type"
)
_TEAM_TOTALS = (
    "SELECT project_id, resource_type, unit, SUM(quantity) AS total_quantity "
    "FROM usage_events WHERE team_id = $1 GROUP BY project_id, resource_type, unit ORDER BY project_id, resource_type"
)


async def project_events(ctx: AuthzContext, project_id: uuid.UUID, limit: int, offset: int) -> list[UsageEvent]:
    """FR4: raw usage_events visible mid-period, not a month-end-only report."""
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(_PROJECT_EVENTS, project_id, limit, offset)
    return [UsageEvent.model_validate(dict(r)) for r in rows]


async def org_totals(ctx: AuthzContext) -> list[UsageTotal]:
    """req/01 FR3 + req/04: the tenant's usage by project and resource type."""
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(_ORG_TOTALS)
    return [UsageTotal.model_validate(dict(r)) for r in rows]


async def team_totals(ctx: AuthzContext, team_id: uuid.UUID) -> list[UsageTotal]:
    """FR1/FR3 (09-usage-billing-two-tier.md): the third rollup tier, the same shape as org_totals."""
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(_TEAM_TOTALS, team_id)
    return [UsageTotal.model_validate(dict(r)) for r in rows]


async def record_usage(
    conn,
    *,
    tenant_id: str,
    project_id,
    resource_type: str,
    quantity,
    unit: str,
    occurred_at,
    source_ref: str,
) -> bool:
    """Idempotent append (FR1/NFR2): a repeat call with the same
    (resource_type, source_ref) no-ops. Returns True if a new row was written."""
    # FR1 (09-usage-billing-two-tier.md): team_id derived from the project
    # at write time, so call sites don't need to look it up themselves.
    team_id = await get_team_id(conn, project_id)
    row = await conn.fetchrow(
        "INSERT INTO usage_events "
        "(tenant_id, project_id, team_id, resource_type, quantity, unit, occurred_at, source_ref, pricing_version) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
        "ON CONFLICT (resource_type, source_ref) DO NOTHING "
        "RETURNING id",
        tenant_id,
        project_id,
        team_id,
        resource_type,
        quantity,
        unit,
        occurred_at,
        source_ref,
        PRICING_VERSION,
    )
    return row is not None
