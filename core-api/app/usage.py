"""Usage metering (req/09-usage-metering.md).

FR3's open question (inference compute-unit normalization formula) is
explicitly unresolved in the spec — we take the doc's own fallback: raw
GPU-seconds, no synthetic unit, pending a real pricing decision. That's
`PRICING_VERSION` below; bump it and every write after the bump carries the
new version, so past invoices stay reproducible from the version that was
active when the usage occurred.
"""
import uuid

from fastapi import APIRouter, Depends, Query

from app.tenancy import get_tenant_id
from app.tenant_conn import TenantConn

router = APIRouter(prefix="/v1", tags=["usage"])

PRICING_VERSION = "v1-raw-gpu-seconds"


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
    # at write time, same join path the 02 backfill migration used, so new
    # rows don't need every call site to look it up themselves.
    row = await conn.fetchrow(
        "INSERT INTO usage_events "
        "(tenant_id, project_id, team_id, resource_type, quantity, unit, occurred_at, source_ref, pricing_version) "
        "VALUES ($1, $2, (SELECT team_id FROM projects WHERE id = $2), $3, $4, $5, $6, $7, $8) "
        "ON CONFLICT (resource_type, source_ref) DO NOTHING "
        "RETURNING id",
        tenant_id,
        project_id,
        resource_type,
        quantity,
        unit,
        occurred_at,
        source_ref,
        PRICING_VERSION,
    )
    return row is not None


@router.get("/projects/{project_id}/usage")
async def project_usage(
    project_id: uuid.UUID,
    tenant_id: str = Depends(get_tenant_id),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    # FR4: raw usage_events visible mid-period, not a month-end-only report.
    async with TenantConn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT id, resource_type, quantity, unit, occurred_at, source_ref, pricing_version "
            "FROM usage_events WHERE project_id = $1 "
            "ORDER BY occurred_at DESC LIMIT $2 OFFSET $3",
            project_id,
            limit,
            offset,
        )
    return [dict(r) for r in rows]


@router.get("/usage")
async def org_usage(tenant_id: str = Depends(get_tenant_id)):
    # req/01 FR3 + req/04: org usage breakdown, by project and resource type.
    async with TenantConn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT project_id, resource_type, unit, SUM(quantity) AS total_quantity "
            "FROM usage_events GROUP BY project_id, resource_type, unit "
            "ORDER BY project_id, resource_type"
        )
    return [dict(r) for r in rows]


@router.get("/teams/{team_id}/usage")
async def team_usage(team_id: uuid.UUID, tenant_id: str = Depends(get_tenant_id)):
    # FR1/FR3 (09-usage-billing-two-tier.md): the third rollup tier — sum
    # across every project in the team, same shape as org_usage above.
    async with TenantConn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT project_id, resource_type, unit, SUM(quantity) AS total_quantity "
            "FROM usage_events WHERE team_id = $1 GROUP BY project_id, resource_type, unit "
            "ORDER BY project_id, resource_type",
            team_id,
        )
    return [dict(r) for r in rows]
