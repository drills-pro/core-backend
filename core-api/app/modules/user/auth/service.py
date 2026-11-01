"""Sign-in bookkeeping and tenant creation for /v1/me and POST /v1/tenants; routes.py only translates HTTP."""
import uuid

import asyncpg
from fastapi import HTTPException

from app.core.auth import Claims
from app.core.authz import TenantRole
from app.core.db import get_pool
from app.core.schemas import slugify
from app.core.tenant_conn import TenantConn
from app.modules.user.auth.constants import DEFAULT_TEAM_NAME, TENANT_SLUG_CONSTRAINT
from app.modules.user.auth.schemas import TenantCreate, TenantCreated
from app.modules.user.teams import create_team
from app.modules.user.tenant_members import add_member, upsert_principal

_MY_TENANTS = "SELECT id, name, slug, role, status FROM my_tenants($1)"
_REPLAY = (
    "SELECT t.id, t.name, t.slug FROM tenant_create_keys k JOIN tenants t ON t.id = k.new_tenant "
    "WHERE k.user_sub = $1 AND k.key = $2"
)
_CLAIM_KEY = "INSERT INTO tenant_create_keys (user_sub, key, new_tenant) VALUES ($1, $2, $3)"
_INSERT_TENANT = "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3) RETURNING slug"
_SLUG_TAKEN = "SELECT true FROM tenants WHERE slug = $1"


async def sign_in(claims: Claims) -> list[asyncpg.Record]:
    """Record the sign-in and return the caller's tenants."""
    async with (await get_pool()).acquire() as conn:
        await upsert_principal(conn, claims)
        return await conn.fetch(_MY_TENANTS, claims.subject)


async def find_created(user_sub: str, idempotency_key: str) -> TenantCreated | None:
    """The tenant an earlier request with this key created, so a retry gets the first answer."""
    async with (await get_pool()).acquire() as conn:
        row = await conn.fetchrow(_REPLAY, user_sub, idempotency_key)
    return None if row is None else TenantCreated(id=row["id"], name=row["name"], slug=row["slug"])


async def _slug_for(conn, name: str, requested: str | None) -> str | None:
    """The slug to insert. A requested one is taken as is (a clash is a 409); otherwise the name's own if free,
    else None, and the tenants_set_slug trigger appends the tenant id's prefix."""
    if requested:
        return requested
    suggested = slugify(name)
    if suggested and not await conn.fetchval(_SLUG_TAKEN, suggested):
        return suggested
    return None


async def create_tenant(claims: Claims, body: TenantCreate, idempotency_key: str) -> TenantCreated:
    """Tenant, owner membership and Default team in one transaction: nothing outside Postgres to undo."""
    # Scoped to the new tenant's own id so the RLS-protected inserts are allowed (tenants has no RLS).
    tenant_id = uuid.uuid4()
    try:
        async with TenantConn(str(tenant_id)) as conn:
            await upsert_principal(conn, claims)
            # Claim the key first: a concurrent retry waits on this row, then fails and rolls back its own tenant.
            await conn.execute(_CLAIM_KEY, claims.subject, idempotency_key, tenant_id)
            created_slug = await conn.fetchval(_INSERT_TENANT, tenant_id, body.name, await _slug_for(conn, body.name, body.slug))
            await add_member(conn, tenant_id, claims.subject, TenantRole.OWNER)
            await create_team(conn, tenant_id, DEFAULT_TEAM_NAME)
    except asyncpg.UniqueViolationError as error:
        if error.constraint_name == TENANT_SLUG_CONSTRAINT:
            raise HTTPException(status_code=409, detail="slug already taken") from None
        raise HTTPException(status_code=409, detail="request in progress, retry") from None  # same key in flight
    return TenantCreated(id=tenant_id, name=body.name, slug=created_slug)
