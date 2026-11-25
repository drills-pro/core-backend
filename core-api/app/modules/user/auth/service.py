"""Sign-in bookkeeping, email verification and tenant creation for /v1/me, /v1/me/email-verification and
POST /v1/tenants; routes.py only translates HTTP."""
import logging
import secrets
import uuid
from http import HTTPStatus

import asyncpg
import httpx

from app.core import secret_files
from app.core.auth import Claims
from app.core.authz import TenantRole
from app.core.db import get_pool
from app.core.problem import Problem
from app.core.schemas import TENANT_SLUG_MAX_LENGTH, slugify
from app.core.settings import settings
from app.core.tenant_conn import TenantConn
from app.integrations import zitadel
from app.modules.user.auth.constants import (
    DEFAULT_SLUG_BASE,
    SLUG_SUFFIX_ALPHABET,
    SLUG_SUFFIX_LENGTH,
    SLUG_SUFFIX_TRIES,
    TENANT_SLUG_CONSTRAINT,
    ZITADEL_VERIFY_URL_TEMPLATE,
    ProblemCode,
)
from app.modules.user.auth.schemas import EmailVerificationStatus, TenantCreate, TenantCreated
from app.modules.user.teams import DEFAULT_TEAM_NAME, create_team
from app.modules.user.tenant_members import add_member, upsert_principal
from app.modules.user.tenants import get_ref, insert_tenant, slug_taken

logger = logging.getLogger(__name__)
# Zitadel unreachable or answering nonsense, or the service key unreadable: the caller can only retry.
_ZITADEL_FAILURES = (httpx.HTTPError, OSError, KeyError, ValueError, secret_files.SecretError)

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


async def slug_available(slug: str) -> bool:
    """One point lookup on the unique slug index; no transaction, no write."""
    async with (await get_pool()).acquire() as conn:
        return not await slug_taken(conn, slug)


async def _slug_for(conn, name: str, requested: str | None) -> str:
    """The slug to insert. A requested one is taken as is (a clash is a 409). Otherwise the name's own slug, or, when another
    workspace has it, the same with a random ending: the user only names the workspace, the handle is ours to make."""
    if requested:
        return requested
    base = slugify(name) or DEFAULT_SLUG_BASE
    if not await slug_taken(conn, base):
        return base
    for _ in range(SLUG_SUFFIX_TRIES):
        ending = "".join(secrets.choice(SLUG_SUFFIX_ALPHABET) for _ in range(SLUG_SUFFIX_LENGTH))
        candidate = f"{base[: TENANT_SLUG_MAX_LENGTH - SLUG_SUFFIX_LENGTH - 1].rstrip('-')}-{ending}"
        if not await slug_taken(conn, candidate):
            return candidate
    raise Problem(HTTPStatus.CONFLICT, ProblemCode.SLUG_TAKEN, "could not find a free handle for this name, try another name")


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


def _verify_url_template() -> str:
    return settings.zitadel.issuer + ZITADEL_VERIFY_URL_TEMPLATE


async def verification_status(claims: Claims) -> EmailVerificationStatus:
    """Zitadel's live answer: the caller's token says what was true when it was issued."""
    try:
        async with zitadel.client() as client:
            return EmailVerificationStatus(verified=await zitadel.email_verified(client, claims.subject))
    except _ZITADEL_FAILURES:
        logger.warning("zitadel user lookup failed for sub %s", claims.subject, exc_info=True)
        raise Problem(HTTPStatus.BAD_GATEWAY, ProblemCode.IDENTITY_UNAVAILABLE, "could not check the email; try again") from None


async def send_verification(claims: Claims) -> None:
    """Check the caller's email in Zitadel and, if it is not verified, have Zitadel mail the verification link.
    409 email_already_verified when it already is: the SPA then signs in again for a token that says so."""
    if not claims.email:
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.NO_EMAIL, "this account has no email address")
    if (await verification_status(claims)).verified:
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.EMAIL_ALREADY_VERIFIED, "email already verified; sign in again")
    try:
        async with zitadel.client() as client:
            await zitadel.send_email_verification(client, claims.subject, _verify_url_template())
    except _ZITADEL_FAILURES:
        logger.warning("zitadel email code failed for sub %s", claims.subject, exc_info=True)
        raise Problem(HTTPStatus.BAD_GATEWAY, ProblemCode.IDENTITY_UNAVAILABLE, "could not send the email; try again") from None
