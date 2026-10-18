import uuid
from datetime import datetime
from enum import StrEnum

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core import audit
from app.core.authz import Action, AuthzContext, Scope, generate_key, require
from app.core.problem import not_found
from app.core.tenant_conn import TenantConn

router = APIRouter(prefix="/v1/projects/{project_id}/api-keys", tags=["api-keys"])


class AuditAction(StrEnum):
    CREATE = "api_key.create"
    REVOKE = "api_key.revoke"


class NewApiKey(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[Scope] = Field(min_length=1)
    model_ids: list[uuid.UUID] = []  # empty = every model in the project
    expires_at: datetime | None = None


@router.post("", status_code=201)
async def create_api_key(
    project_id: uuid.UUID, body: NewApiKey, ctx: AuthzContext = Depends(require(Action.API_KEY_MANAGE))
):
    """The secret is in this response only; the database keeps its hash."""
    key = generate_key()
    scopes, model_ids = sorted({s.value for s in body.scopes}), sorted(set(body.model_ids))
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "INSERT INTO api_keys (tenant_id, project_id, name, prefix, key_hash, created_by, expires_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id, created_at",
            ctx.tenant_id, project_id, body.name, key.prefix, key.key_hash, ctx.actor, body.expires_at,
        )
        await conn.execute(
            "INSERT INTO api_key_scopes (key_id, tenant_id, scope) SELECT $1, $2, unnest($3::text[])",
            row["id"], ctx.tenant_id, scopes,
        )
        await conn.execute(
            "INSERT INTO api_key_models (key_id, tenant_id, model_id) SELECT $1, $2, unnest($3::uuid[])",
            row["id"], ctx.tenant_id, model_ids,
        )
        await audit.record(
            conn, tenant_id=ctx.tenant_id, project_id=project_id, actor=ctx.actor,
            action=AuditAction.CREATE, entity_type="api_key", entity_id=row["id"],
        )
    return {
        "id": row["id"], "name": body.name, "prefix": key.prefix, "scopes": scopes, "model_ids": model_ids,
        "expires_at": body.expires_at, "created_at": row["created_at"], "secret": key.secret,
    }


@router.get("")
async def list_api_keys(project_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.API_KEY_MANAGE))):
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT k.id, k.name, k.prefix, k.created_by, k.created_at, k.expires_at, k.revoked_at, "
            "COALESCE((SELECT array_agg(scope ORDER BY scope) FROM api_key_scopes WHERE key_id = k.id), '{}') AS scopes, "
            "COALESCE((SELECT array_agg(model_id ORDER BY model_id) FROM api_key_models WHERE key_id = k.id), '{}') AS model_ids "
            "FROM api_keys k WHERE k.project_id = $1 ORDER BY k.created_at",
            project_id,
        )
    return [dict(r) for r in rows]


@router.delete("/{key_id}", status_code=204)
async def revoke_api_key(
    project_id: uuid.UUID, key_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.API_KEY_MANAGE))
):
    """Takes effect on the key's next request. Revoking twice keeps the first revocation time."""
    async with TenantConn(ctx.tenant_id) as conn:
        revoked = await conn.fetchval(
            "UPDATE api_keys SET revoked_at = COALESCE(revoked_at, now()) WHERE id = $1 AND project_id = $2 RETURNING id",
            key_id, project_id,
        )
        if revoked is None:
            raise not_found()
        await audit.record(
            conn, tenant_id=ctx.tenant_id, project_id=project_id, actor=ctx.actor,
            action=AuditAction.REVOKE, entity_type="api_key", entity_id=key_id,
        )
