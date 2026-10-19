"""Who am I, and make my first tenant. Login itself is Zitadel's hosted OIDC flow: the SPA sends the
access token, core/auth.py verifies it, core/tenancy.py checks membership (ADR-021)."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core import metrics, rate_limit
from app.core.auth import ZITADEL_ISSUER, Claims, get_profile_claims
from app.core.authz import TenantRole, public
from app.core.db import get_pool
from app.core.tenant_conn import TenantConn

router = APIRouter(prefix="/v1", tags=["auth"])


class TenantCreate(BaseModel):
    name: str


def _client_ip(request: Request) -> str:
    # Traefik appends the peer it saw, so the last hop is the one the client cannot forge.
    # ponytail: assumes exactly one trusted proxy in front; revisit if a CDN is added.
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[-1].strip() or (request.client.host if request.client else "?")


async def _upsert_principal(conn, claims: Claims) -> None:
    # Claims may be absent from the access token; never overwrite a known value with nothing.
    # xmax is 0 only on a freshly inserted row, so it tells a first sign-in from a returning one.
    inserted = await conn.fetchval(
        "INSERT INTO principals (issuer, sub, email, display_name) VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (issuer, sub) DO UPDATE SET email = COALESCE(EXCLUDED.email, principals.email), "
        "display_name = COALESCE(EXCLUDED.display_name, principals.display_name), last_seen_at = now() "
        "RETURNING (xmax = 0) AS inserted",
        ZITADEL_ISSUER, claims.subject, claims.email, claims.display_name,
    )
    if inserted:
        metrics.auth_principals_created_total.add(1)


@router.get("/me", dependencies=[Depends(public)])  # self-scoped: any valid token, no tenant yet
async def me(claims: Claims = Depends(get_profile_claims)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _upsert_principal(conn, claims)
        tenants = await conn.fetch("SELECT id, name, role FROM my_tenants($1)", claims.subject)
    return {"sub": claims.subject, "email": claims.email, "name": claims.display_name,
            "tenants": [dict(t, id=str(t["id"])) for t in tenants]}


@router.post("/tenants", status_code=201, dependencies=[Depends(public)])  # creates the caller's own tenant
async def create_tenant(body: TenantCreate, request: Request, claims: Claims = Depends(get_profile_claims)):
    """Tenant, owner membership and Default team in one transaction: nothing outside Postgres to undo."""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if not claims.email_verified:
        raise HTTPException(status_code=403, detail="verify your email before creating a tenant")
    if not (
        await rate_limit.check(f"tenant:sub:{claims.subject}", 3)
        and await rate_limit.check(f"tenant:ip:{_client_ip(request)}", 5)
    ):
        raise HTTPException(status_code=429, detail="too many attempts")

    # Scoped to the new tenant's own id so the RLS-protected inserts are allowed (tenants has no RLS).
    tenant_id = uuid.uuid4()
    async with TenantConn(str(tenant_id)) as conn:
        await _upsert_principal(conn, claims)
        await conn.execute("INSERT INTO tenants (id, name) VALUES ($1, $2)", tenant_id, name)
        await conn.execute(
            "INSERT INTO tenant_members (tenant_id, user_sub, role) VALUES ($1, $2, $3)",
            tenant_id, claims.subject, TenantRole.OWNER,
        )
        await conn.execute("INSERT INTO teams (tenant_id, name) VALUES ($1, 'Default')", tenant_id)
    return {"id": str(tenant_id), "name": name, "role": TenantRole.OWNER}
