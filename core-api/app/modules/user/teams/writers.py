"""What other contexts may ask of teams, on the caller's connection and transaction."""
import uuid

from app.modules.user.teams.schemas import Team

_INSERT = "INSERT INTO teams (tenant_id, name) VALUES ($1, $2) RETURNING id, name, created_at"


async def create_team(conn, tenant_id: uuid.UUID | str, name: str) -> Team:
    return Team.model_validate(dict(await conn.fetchrow(_INSERT, tenant_id, name)))
