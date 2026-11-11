import uuid

from app.core.authz import AuthzContext
from app.core.tenant_conn import TenantConn
from app.modules.user.job_events import JobEvent
from app.modules.user.notifications.constants import NOTIFICATIONS_LIMIT
from app.modules.user.notifications.schemas import Notifications

_CURSOR = "COALESCE((SELECT last_seq FROM notification_cursors WHERE project_id = $1 AND user_sub = $2), 0)"
_UNREAD = f"SELECT count(*) FROM events WHERE project_id = $1 AND seq > {_CURSOR}"
_ITEMS = (
    "SELECT seq, kind, entity_id, status, occurred_at FROM events "
    f"WHERE project_id = $1 AND seq > {_CURSOR} ORDER BY seq DESC LIMIT $3"
)
# A cursor only moves forward: reading an older page later never marks newer events unread again.
_MARK = (
    "INSERT INTO notification_cursors (tenant_id, project_id, user_sub, last_seq) VALUES ($3, $1, $2, $4) "
    "ON CONFLICT (project_id, user_sub) DO UPDATE SET last_seq = GREATEST(notification_cursors.last_seq, EXCLUDED.last_seq)"
)


async def list_unread(ctx: AuthzContext, project_id: uuid.UUID) -> Notifications:
    async with TenantConn(ctx.tenant_id) as conn:
        unread = await conn.fetchval(_UNREAD, project_id, ctx.actor)
        rows = await conn.fetch(_ITEMS, project_id, ctx.actor, NOTIFICATIONS_LIMIT)
    return Notifications(unread=unread, items=[JobEvent.model_validate(dict(r)) for r in rows])


async def mark_read(ctx: AuthzContext, project_id: uuid.UUID, seq: int) -> None:
    async with TenantConn(ctx.tenant_id) as conn:
        await conn.execute(_MARK, project_id, ctx.actor, ctx.tenant_id, seq)
