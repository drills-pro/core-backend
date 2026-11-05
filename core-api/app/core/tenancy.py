import uuid
from http import HTTPStatus

from fastapi import Header
from starlette.concurrency import run_in_threadpool

from app.core.settings import settings
from app.core.auth import Claims, verify_token
from app.core.db import get_pool
from app.core.problem import Problem, ProblemCode, not_found
from app.core.tracing import tag_current_span


async def _member_tenant(sub: str, requested: str | None) -> str:
    """The tenant a verified user acts in. Membership is read from Postgres on every request, so
    removing a member takes effect at once rather than at token expiry (ADR-021). No membership is
    a 404: it must not reveal whether the tenant exists."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if requested:
            try:
                tenant_id = uuid.UUID(requested)
            except ValueError:
                raise not_found() from None
            if await conn.fetchval("SELECT member_role($1, $2)", sub, tenant_id) is None:
                raise not_found()
            return str(tenant_id)
        # No header: a user with exactly one tenant needs none, which keeps the common case seamless.
        mine = await conn.fetch("SELECT id FROM my_tenants($1)", sub)
    if not mine:
        raise Problem(HTTPStatus.FORBIDDEN, ProblemCode.NO_TENANT, "no tenant yet: POST /v1/tenants")
    if len(mine) > 1:
        raise Problem(HTTPStatus.BAD_REQUEST, ProblemCode.TENANT_REQUIRED, "X-Tenant-Id is required: you belong to several tenants")
    return str(mine[0]["id"])


async def _get_claims(authorization: str | None) -> Claims | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return await run_in_threadpool(verify_token, authorization.removeprefix("Bearer "))


async def get_tenant_id(
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> str:
    """Real Zitadel JWT when present (req/02 FR1: verify a token, never
    trust a client-supplied header) — this is what the frontend's genuine
    OIDC login sends. Falls back to the X-Tenant-Id dev header when no
    Authorization header is present, so every existing curl-based
    flow keep working unchanged; both paths are equally real, just
    different callers."""
    claims = await _get_claims(authorization)
    if claims is not None:
        tenant_id = await _member_tenant(claims.subject, x_tenant_id)
        tag_current_span(tenant_id=tenant_id)
        return tenant_id

    if not settings.auth.dev_headers or not x_tenant_id:
        raise Problem(HTTPStatus.UNAUTHORIZED, ProblemCode.UNAUTHENTICATED, "missing Authorization bearer token or X-Tenant-Id")
    tag_current_span(tenant_id=x_tenant_id)
    return x_tenant_id


async def get_actor(
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> str:
    claims = await _get_claims(authorization)
    if claims is not None:
        return claims.subject
    if not settings.auth.dev_headers:
        raise Problem(HTTPStatus.UNAUTHORIZED, ProblemCode.UNAUTHENTICATED, "missing Authorization bearer token")
    return x_user_id or "dev-user"
