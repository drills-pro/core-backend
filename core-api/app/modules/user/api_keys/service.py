import uuid
from http import HTTPStatus

from app.core import audit
from app.core.authz import AuthzContext, generate_key
from app.core.problem import Problem, not_found
from app.core.tenant_conn import TenantConn
from app.modules.user.api_keys.constants import UNKNOWN_MODEL_DETAIL, AuditAction, EntityType, ProblemCode
from app.modules.user.api_keys.schemas import CreatedApiKey, ListedApiKey, NewApiKey

_INSERT_KEY = (
    "INSERT INTO api_keys (tenant_id, project_id, name, prefix, key_hash, created_by, expires_at, rate_limit_per_minute) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id, created_at"
)
_INSERT_SCOPES = "INSERT INTO api_key_scopes (key_id, tenant_id, scope) SELECT $1, $2, unnest($3::text[])"
# Only models of this project: an id from another project or tenant is counted out and refused below.
_INSERT_MODELS = (
    "WITH i AS (INSERT INTO api_key_models (key_id, tenant_id, model_id) "
    "SELECT $1, $2, m.id FROM models m WHERE m.project_id = $4 AND m.id = ANY($3::uuid[]) RETURNING 1) SELECT count(*) FROM i"
)
_LIST = (
    "SELECT k.id, k.name, k.prefix, k.created_by, k.created_at, k.expires_at, k.revoked_at, k.last_used_at, k.rate_limit_per_minute, "
    "COALESCE((SELECT array_agg(scope ORDER BY scope) FROM api_key_scopes WHERE key_id = k.id), '{}') AS scopes, "
    "COALESCE((SELECT array_agg(model_id ORDER BY model_id) FROM api_key_models WHERE key_id = k.id), '{}') AS model_ids "
    "FROM api_keys k WHERE k.project_id = $1 ORDER BY k.created_at"
)
_REVOKE = "UPDATE api_keys SET revoked_at = COALESCE(revoked_at, now()) WHERE id = $1 AND project_id = $2 RETURNING id"


async def create(ctx: AuthzContext, project_id: uuid.UUID, body: NewApiKey) -> CreatedApiKey:
    """The secret is in this result only; the database keeps its hash."""
    key = generate_key()
    scopes, model_ids = sorted({s.value for s in body.scopes}), sorted(set(body.model_ids))
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            _INSERT_KEY, ctx.tenant_id, project_id, body.name, key.prefix, key.key_hash, ctx.actor, body.expires_at,
            body.rate_limit_per_minute,
        )
        await conn.execute(_INSERT_SCOPES, row["id"], ctx.tenant_id, scopes)
        if model_ids and await conn.fetchval(_INSERT_MODELS, row["id"], ctx.tenant_id, model_ids, project_id) != len(model_ids):
            raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.UNKNOWN_MODEL, UNKNOWN_MODEL_DETAIL)  # rolls the key back
        await audit.record_for(conn, ctx, project_id, AuditAction.CREATE, EntityType.API_KEY, row["id"])
    return CreatedApiKey(
        id=row["id"], name=body.name, prefix=key.prefix, scopes=scopes, model_ids=model_ids,
        expires_at=body.expires_at, created_at=row["created_at"], secret=key.secret, rate_limit_per_minute=body.rate_limit_per_minute,
    )


async def list_keys(ctx: AuthzContext, project_id: uuid.UUID) -> list[ListedApiKey]:
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(_LIST, project_id)
    return [ListedApiKey.model_validate(dict(r)) for r in rows]


async def revoke(ctx: AuthzContext, project_id: uuid.UUID, key_id: uuid.UUID) -> None:
    """Takes effect on the key's next request. Revoking twice keeps the first revocation time."""
    async with TenantConn(ctx.tenant_id) as conn:
        if await conn.fetchval(_REVOKE, key_id, project_id) is None:
            raise not_found()
        await audit.record_for(conn, ctx, project_id, AuditAction.REVOKE, EntityType.API_KEY, key_id)
