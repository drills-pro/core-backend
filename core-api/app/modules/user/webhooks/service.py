import uuid
from http import HTTPStatus

from app.core import audit
from app.core.authz import AuthzContext
from app.core.problem import Problem, not_found
from app.core.tenant_conn import TenantConn
from app.modules.user.webhooks import signing
from app.modules.user.webhooks.constants import UNAVAILABLE_DETAIL, AuditAction, EntityType, ProblemCode
from app.modules.user.webhooks.schemas import CreatedWebhook, NewWebhook, Webhook

# The cursor starts at the project's newest event: a new endpoint hears what happens from now on, not the history.
_INSERT = (
    "INSERT INTO webhook_endpoints (tenant_id, project_id, url, created_by, last_seq) "
    "VALUES ($1, $2, $3, $4, COALESCE((SELECT max(seq) FROM events WHERE project_id = $2), 0)) RETURNING id, created_at"
)
_LIST = "SELECT id, url, created_at, disabled_at FROM webhook_endpoints WHERE project_id = $1 ORDER BY created_at"
_DELETE = "DELETE FROM webhook_endpoints WHERE id = $1 AND project_id = $2 RETURNING id"


async def create(ctx: AuthzContext, project_id: uuid.UUID, body: NewWebhook) -> CreatedWebhook:
    reason = signing.url_problem(body.url)
    if reason:
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.BAD_URL, reason)
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(_INSERT, ctx.tenant_id, project_id, body.url, ctx.actor)
        try:
            secret = signing.endpoint_secret(row["id"])
        except signing.SigningUnavailable:
            raise Problem(HTTPStatus.SERVICE_UNAVAILABLE, ProblemCode.UNAVAILABLE, UNAVAILABLE_DETAIL) from None  # rolls the row back
        await audit.record_for(conn, ctx, project_id, AuditAction.CREATE, EntityType.WEBHOOK, row["id"])
    return CreatedWebhook(id=row["id"], url=body.url, created_at=row["created_at"], secret=secret)


async def list_webhooks(ctx: AuthzContext, project_id: uuid.UUID) -> list[Webhook]:
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(_LIST, project_id)
    return [Webhook.model_validate(dict(r)) for r in rows]


async def delete(ctx: AuthzContext, project_id: uuid.UUID, webhook_id: uuid.UUID) -> None:
    async with TenantConn(ctx.tenant_id) as conn:
        if await conn.fetchval(_DELETE, webhook_id, project_id) is None:
            raise not_found()
        await audit.record_for(conn, ctx, project_id, AuditAction.DELETE, EntityType.WEBHOOK, webhook_id)
