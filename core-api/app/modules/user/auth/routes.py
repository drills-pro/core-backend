"""Who am I, and make my first tenant. Login itself is Zitadel's hosted OIDC flow: the SPA sends the
access token, core/auth.py verifies it, core/tenancy.py checks membership (ADR-021)."""
import uuid

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from app.core import rate_limit
from app.core.auth import ZITADEL_ISSUER, Claims, get_profile_claims
from app.core.authz import TenantRole, allowed_actions, public
from app.core.authz.policy import TENANT_ACTIONS
from app.core.db import get_pool
from app.core.schemas import slugify
from app.core.tenant_conn import TenantConn
from app.modules.user.auth.constants import (
    DEFAULT_TEAM_NAME,
    IDEMPOTENCY_HEADER,
    IDEMPOTENCY_KEY_MAX_LENGTH,
    TENANT_CREATE_IP_LIMIT,
    TENANT_CREATE_IP_RATE_KEY,
    TENANT_CREATE_USER_LIMIT,
    TENANT_CREATE_USER_RATE_KEY,
    TENANT_SLUG_CONSTRAINT,
)
from app.modules.user.auth.schemas import Me, TenantCreate, TenantCreated, TenantSummary

router = APIRouter(prefix="/v1", tags=["auth"])


def _client_ip(request: Request) -> str:
    # Traefik appends the peer it saw, so the last hop is the one the client cannot forge.
    # ponytail: assumes exactly one trusted proxy in front; revisit if a CDN is added.
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[-1].strip() or (request.client.host if request.client else "?")


async def _upsert_principal(conn, claims: Claims) -> None:
    # Claims may be absent from the access token; never overwrite a known value with nothing.
    await conn.execute(
        "INSERT INTO principals (issuer, sub, email, display_name) VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (issuer, sub) DO UPDATE SET email = COALESCE(EXCLUDED.email, principals.email), "
        "display_name = COALESCE(EXCLUDED.display_name, principals.display_name), last_seen_at = now()",
        ZITADEL_ISSUER, claims.subject, claims.email, claims.display_name,
    )


def _capabilities(role: str) -> list[str]:
    return sorted(a.value for a in allowed_actions(TenantRole(role), None, None, ()) & TENANT_ACTIONS)


@router.get("/me", dependencies=[Depends(public)], response_model=Me)  # self-scoped: any valid token, no tenant yet
async def me(claims: Claims = Depends(get_profile_claims)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _upsert_principal(conn, claims)
        tenants = await conn.fetch("SELECT id, name, slug, role, status FROM my_tenants($1)", claims.subject)
    # A user may belong to many tenants; capabilities are the tenant-level actions of the role held in each.
    return Me(
        sub=claims.subject, email=claims.email, email_verified=claims.email_verified, name=claims.display_name,
        tenants=[TenantSummary(**dict(t), capabilities=_capabilities(t["role"])) for t in tenants],
    )


async def _slug_for(conn, name: str, requested: str | None) -> str | None:
    """The slug to insert. A requested one is taken as is (a clash is a 409); otherwise the name's own if free,
    else None, and the tenants_set_slug trigger appends the tenant id's prefix."""
    if requested:
        return requested
    suggested = slugify(name)
    if suggested and not await conn.fetchval("SELECT true FROM tenants WHERE slug = $1", suggested):
        return suggested
    return None


@router.post("/tenants", status_code=201, dependencies=[Depends(public)], response_model=TenantCreated)  # creates the caller's own tenant
async def create_tenant(
    body: TenantCreate,
    request: Request,
    claims: Claims = Depends(get_profile_claims),
    idempotency_key: str = Header(..., alias=IDEMPOTENCY_HEADER, min_length=1, max_length=IDEMPOTENCY_KEY_MAX_LENGTH),
):
    """Tenant, owner membership and Default team in one transaction: nothing outside Postgres to undo."""
    name = body.name
    if not claims.email_verified:
        raise HTTPException(status_code=403, detail="verify your email before creating a tenant")
    pool = await get_pool()
    async with pool.acquire() as conn:  # a retry gets the first answer, and costs no rate-limit budget
        replay = await conn.fetchrow(
            "SELECT t.id, t.name, t.slug FROM tenant_create_keys k JOIN tenants t ON t.id = k.new_tenant "
            "WHERE k.user_sub = $1 AND k.key = $2", claims.subject, idempotency_key)
    if replay is not None:
        return TenantCreated(id=replay["id"], name=replay["name"], slug=replay["slug"])
    if not (
        await rate_limit.check(TENANT_CREATE_USER_RATE_KEY.format(claims.subject), TENANT_CREATE_USER_LIMIT)
        and await rate_limit.check(TENANT_CREATE_IP_RATE_KEY.format(_client_ip(request)), TENANT_CREATE_IP_LIMIT)
    ):
        raise HTTPException(status_code=429, detail="too many attempts")

    # Scoped to the new tenant's own id so the RLS-protected inserts are allowed (tenants has no RLS).
    tenant_id = uuid.uuid4()
    try:
        async with TenantConn(str(tenant_id)) as conn:
            await _upsert_principal(conn, claims)
            # Claim the key first: a concurrent retry waits on this row, then fails and rolls back its own tenant.
            await conn.execute("INSERT INTO tenant_create_keys (user_sub, key, new_tenant) VALUES ($1, $2, $3)",
                               claims.subject, idempotency_key, tenant_id)
            created_slug = await conn.fetchval(
                "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3) RETURNING slug",
                tenant_id, name, await _slug_for(conn, name, body.slug))
            await conn.execute(
                "INSERT INTO tenant_members (tenant_id, user_sub, role) VALUES ($1, $2, $3)",
                tenant_id, claims.subject, TenantRole.OWNER,
            )
            await conn.execute("INSERT INTO teams (tenant_id, name) VALUES ($1, $2)", tenant_id, DEFAULT_TEAM_NAME)
    except asyncpg.UniqueViolationError as error:
        if error.constraint_name == TENANT_SLUG_CONSTRAINT:
            raise HTTPException(status_code=409, detail="slug already taken") from None
        raise HTTPException(status_code=409, detail="request in progress, retry") from None  # same key in flight
    return TenantCreated(id=tenant_id, name=name, slug=created_slug)
