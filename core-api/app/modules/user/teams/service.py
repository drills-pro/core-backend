import uuid

from app.core.authz import AuthzContext
from app.core.problem import not_found
from app.core.tenant_conn import TenantConn
from app.modules.user.teams import writers
from app.modules.user.teams.schemas import Team

_LIST = "SELECT id, name, created_at FROM teams ORDER BY created_at LIMIT $1 OFFSET $2"
_GET = "SELECT id, name, created_at FROM teams WHERE id = $1"


async def list_teams(ctx: AuthzContext, limit: int, offset: int) -> list[Team]:
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(_LIST, limit, offset)
    return [Team.model_validate(dict(r)) for r in rows]


async def create_team(ctx: AuthzContext, name: str) -> Team:
    """FR2 (02): every project belongs to exactly one team; a tenant can add
    more teams beyond the bootstrap-seeded "Default" one at any time."""
    async with TenantConn(ctx.tenant_id) as conn:
        return await writers.create_team(conn, ctx.tenant_id, name)


async def get_team(ctx: AuthzContext, team_id: uuid.UUID) -> Team:
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(_GET, team_id)
    if row is None:
        raise not_found()
    return Team.model_validate(dict(row))
