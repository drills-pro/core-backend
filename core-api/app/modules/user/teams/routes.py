import uuid

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.tenancy import get_tenant_id
from app.core.tenant_conn import TenantConn

router = APIRouter(prefix="/v1/teams", tags=["teams"])


@router.get("")
async def list_teams(
    tenant_id: str = Depends(get_tenant_id),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    async with TenantConn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT id, name, created_at FROM teams ORDER BY created_at LIMIT $1 OFFSET $2",
            limit,
            offset,
        )
    return [dict(r) for r in rows]


@router.post("", status_code=201)
async def create_team(name: str, tenant_id: str = Depends(get_tenant_id)):
    # FR2 (02): every project belongs to exactly one team; a tenant can add
    # more teams beyond the bootstrap-seeded "Default" one at any time.
    async with TenantConn(tenant_id) as conn:
        row = await conn.fetchrow(
            "INSERT INTO teams (tenant_id, name) VALUES ($1, $2) "
            "RETURNING id, name, created_at",
            tenant_id,
            name,
        )
    return dict(row)


@router.get("/{team_id}")
async def get_team(team_id: uuid.UUID, tenant_id: str = Depends(get_tenant_id)):
    async with TenantConn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT id, name, created_at FROM teams WHERE id = $1", team_id
        )
    if row is None:
        raise HTTPException(status_code=404, detail="team not found")
    return dict(row)
