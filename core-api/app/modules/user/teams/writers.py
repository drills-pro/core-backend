"""What other contexts may ask of teams, on the caller's connection and transaction."""
import uuid

_INSERT = "INSERT INTO teams (tenant_id, name) VALUES ($1, $2)"


async def create_team(conn, tenant_id: uuid.UUID, name: str) -> None:
    await conn.execute(_INSERT, tenant_id, name)
