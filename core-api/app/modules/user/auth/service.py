"""Sign-in bookkeeping and tenant creation for /v1/me and POST /v1/tenants; routes.py only translates HTTP."""
import uuid
from http import HTTPStatus

import asyncpg

from app.core.auth import Claims
from app.core.authz import TenantRole
from app.core.db import get_pool
from app.core.problem import Problem
from app.core.schemas import slugify
from app.core.tenant_conn import TenantConn
from app.modules.user.auth.constants import TENANT_SLUG_CONSTRAINT, ProblemCode
from app.modules.user.auth.schemas import TenantCreate, TenantCreated
from app.modules.user.teams import DEFAULT_TEAM_NAME, create_team
from app.modules.user.tenant_members import add_member, upsert_principal
from app.modules.user.tenants import get_ref, insert_tenant, slug_taken

_MY_TENANTS = "SELECT id, name, slug, role, status FROM my_tenants($1)"
_REPLAY = "SELECT new_tenant FROM tenant_create_keys WHERE user_sub = $1 AND key = $2"
_CLAIM_KEY = "INSERT INTO tenant_create_keys (user_sub, key, new_tenant) VALUES ($1, $2, $3)"


async def sign_in(claims: Claims) -> list[asyncpg.Record]:
    """Record the sign-in and return the caller's tenants."""
    async with (await get_pool()).acquire() as conn:
        await upsert_principal(conn, claims)
        return await conn.fetch(_MY_TENANTS, claims.subject)


async def find_created(user_sub: str, idempotency_key: str) -> TenantCreated | None:
    """The tenant an earlier request with this key created, so a retry gets the first answer."""
    async with (await get_pool()).acquire() as conn:
        key_row = await conn.fetchrow(_REPLAY, user_sub, idempotency_key)
        ref = None if key_row is None else await get_ref(conn, key_row["new_tenant"])
    return None if ref is None else TenantCreated(id=ref.id, name=ref.name, slug=ref.slug)


async def _slug_for(conn, name: str, requested: str | None) -> str | None:
    """The slug to insert. A requested one is taken as is (a clash is a 409); otherwise the name's own if free,
    else None, and the tenants_set_slug trigger appends the tenant id's prefix."""
    if requested:
        return requested
    suggested = slugify(name)
    if suggested and not await slug_taken(conn, suggested):
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
            created_slug = await insert_tenant(conn, tenant_id, body.name, await _slug_for(conn, body.name, body.slug))
            await add_member(conn, tenant_id, claims.subject, TenantRole.OWNER)
            await create_team(conn, tenant_id, DEFAULT_TEAM_NAME)
    except asyncpg.UniqueViolationError as error:
        if error.constraint_name == TENANT_SLUG_CONSTRAINT:
            raise Problem(HTTPStatus.CONFLICT, ProblemCode.SLUG_TAKEN, "slug already taken") from None
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.REQUEST_IN_PROGRESS, "request in progress, retry") from None  # same key in flight
    return TenantCreated(id=tenant_id, name=body.name, slug=created_slug)
