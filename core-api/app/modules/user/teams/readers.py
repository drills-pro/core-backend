"""What other contexts may ask of teams, on the caller's connection and transaction."""
import uuid

_FIRST = "SELECT id FROM teams ORDER BY created_at LIMIT 1"


async def get_default_team_id(conn) -> uuid.UUID | None:
    """The tenant's oldest team (the seeded Default one); None when it has none."""
    return await conn.fetchval(_FIRST)
