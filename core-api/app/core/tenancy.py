import uuid
from http import HTTPStatus

from fastapi import Depends, Header
from starlette.concurrency import run_in_threadpool

from app.core.auth import Claims, dev_header, verify_token
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


TENANT_HEADER = "X-Tenant-Id"
USER_HEADER = "X-User-Id"
DEV_USER = "dev-user"


async def get_optional_claims(authorization: str | None = Header(default=None)) -> Claims | None:
    """The verified token of a bearer request, None for any other. A dependency, so FastAPI verifies it once per request
    however many of get_tenant_id, get_actor and the like depend on it."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return await run_in_threadpool(verify_token, authorization.removeprefix("Bearer "))


async def resolve_tenant_id(claims: Claims | None, x_tenant_id: str | None) -> str:
    """Real Zitadel JWT when present (req/02 FR1: verify a token, never
    trust a client-supplied header) — this is what the frontend's genuine
    OIDC login sends. Falls back to the X-Tenant-Id dev header when no
    Authorization header is present, so every existing curl-based
    flow keep working unchanged; both paths are equally real, just
    different callers."""
    if claims is not None:
        tenant_id = await _member_tenant(claims.subject, x_tenant_id)
    else:
        tenant_id = dev_header(x_tenant_id, TENANT_HEADER)
    tag_current_span(tenant_id=tenant_id)
    return tenant_id


def resolve_actor(claims: Claims | None, x_user_id: str | None) -> str:
    return claims.subject if claims is not None else dev_header(x_user_id, USER_HEADER, default=DEV_USER)


async def get_tenant_id(
    claims: Claims | None = Depends(get_optional_claims),
    x_tenant_id: str | None = Header(default=None),
) -> str:
    return await resolve_tenant_id(claims, x_tenant_id)


async def get_actor(
    claims: Claims | None = Depends(get_optional_claims),
    x_user_id: str | None = Header(default=None),
) -> str:
    return resolve_actor(claims, x_user_id)
