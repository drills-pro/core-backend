"""HTTP read surface for usage metering. The writer other modules
call lives in app/modules/usage/service.py.
"""
import uuid

from fastapi import APIRouter, Depends, Query

from app.core.tenancy import get_tenant_id
from app.core.tenant_conn import TenantConn

router = APIRouter(prefix="/v1", tags=["usage"])


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
