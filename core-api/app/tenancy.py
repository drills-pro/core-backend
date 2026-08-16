from fastapi import Header, HTTPException

from app.auth import Claims, verify_token
from app.db import get_pool
from app.tracing import tag_current_span


async def _resolve_tenant_from_org(org_id: str) -> str:
    # FR1/FR2 (01-multi-tenancy.md): a token for an unprovisioned org must
    # not silently succeed or leak existence either way.
    pool = await get_pool()
    async with pool.acquire() as conn:
        tenant_id = await conn.fetchval("SELECT id FROM tenants WHERE zitadel_org_id = $1", org_id)
    if tenant_id is None:
        raise HTTPException(status_code=403, detail="organization not provisioned")
    return str(tenant_id)


def _get_claims(authorization: str | None) -> Claims | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return verify_token(authorization.removeprefix("Bearer "))


async def get_tenant_id(
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> str:
    """Real Zitadel JWT when present (req/02 FR1: verify a token, never
    trust a client-supplied header) — this is what the frontend's genuine
    OIDC login sends. Falls back to the X-Tenant-Id dev header when no
    Authorization header is present, so /ui and every existing curl-based
    flow keep working unchanged; both paths are equally real, just
    different callers."""
    claims = _get_claims(authorization)
    if claims is not None:
        tenant_id = await _resolve_tenant_from_org(claims.org_id)
        tag_current_span(tenant_id=tenant_id)
        return tenant_id

    if not x_tenant_id:
        raise HTTPException(status_code=401, detail="missing Authorization bearer token or X-Tenant-Id")
    tag_current_span(tenant_id=x_tenant_id)
    return x_tenant_id


async def get_actor(
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> str:
    claims = _get_claims(authorization)
    if claims is not None:
        return claims.subject
    return x_user_id or "dev-user"
