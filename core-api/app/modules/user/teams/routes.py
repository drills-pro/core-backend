import uuid

from fastapi import APIRouter, Depends, Query

from app.core.authz import Action, AuthzContext, require
from app.core.problem import not_found
from app.core.tenant_conn import TenantConn
from app.modules.user.teams import writers

router = APIRouter(prefix="/v1/teams", tags=["teams"])


@router.get("")
async def list_teams(
    ctx: AuthzContext = Depends(require(Action.PROJECT_LIST)),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT id, name, created_at FROM teams ORDER BY created_at LIMIT $1 OFFSET $2",
            limit,
            offset,
        )
    return [dict(r) for r in rows]


@router.post("", status_code=201)
async def create_team(name: str, ctx: AuthzContext = Depends(require(Action.TENANT_ADMIN))):
    # FR2 (02): every project belongs to exactly one team; a tenant can add
    # more teams beyond the bootstrap-seeded "Default" one at any time.
    async with TenantConn(ctx.tenant_id) as conn:
        team = await writers.create_team(conn, ctx.tenant_id, name)
    return team.model_dump()


@router.get("/{team_id}")
async def get_team(team_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.PROJECT_LIST))):
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT id, name, created_at FROM teams WHERE id = $1", team_id
        )
    if row is None:
        raise not_found()
    return dict(row)
