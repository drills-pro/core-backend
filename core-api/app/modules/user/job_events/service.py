import uuid

from app.core.authz import AuthzContext
from app.core.tenant_conn import TenantConn
from app.modules.user.job_events.schemas import JobEvent

_LIST = (
    "SELECT seq, kind, entity_id, status, occurred_at FROM events "
    "WHERE project_id = $1 AND seq > $2 ORDER BY seq LIMIT $3"
)


async def list_events(ctx: AuthzContext, project_id: uuid.UUID, after: int, limit: int) -> list[JobEvent]:
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(_LIST, project_id, after, limit)
    return [JobEvent.model_validate(dict(r)) for r in rows]
