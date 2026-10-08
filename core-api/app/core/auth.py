"""JWT verification against Zitadel (req/02-auth-identity.md FR1/FR3/NFR2).

Zitadel puts the org (=tenant, see req/01) on the `urn:zitadel:iam:user:resourceowner:id`
claim, and per-project roles on `urn:zitadel:iam:org:project:roles` (a dict of
role -> {org_id: org_name}). Both are stable, documented Zitadel claim keys,
not something to reverse-engineer per token.
"""
import time

import jwt
from fastapi import Depends, Header, HTTPException
from jwt import PyJWKClient

from app.core import config, db

ZITADEL_ISSUER = config.ZITADEL_ISSUER
# Real tokens carry this as three flat claims (…:id, …:name, …:primary_domain),
# not a nested object under the bare "urn:...:resourceowner" key — verified
# against a live token, contradicting the docs' apparent structure.
ORG_ID_CLAIM = "urn:zitadel:iam:user:resourceowner:id"
ROLES_CLAIM = "urn:zitadel:iam:org:project:roles"

# NFR2: cache JWKS, don't fetch on every request. PyJWKClient caches by
# default (lifespan=300s); one client for the process is enough.
_jwks_client: PyJWKClient | None = None


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(f"{ZITADEL_ISSUER}/oauth/v2/keys", cache_keys=True)
    return _jwks_client


class Claims:
    def __init__(self, raw: dict):
        self.raw = raw
        self.subject: str = raw["sub"]
        self.org_id: str = raw[ORG_ID_CLAIM]
        self.roles: set[str] = set(raw.get(ROLES_CLAIM, {}).keys())


def decode_zitadel_jwt(token: str, audience: str | None = None) -> dict:
    """Signature (against Zitadel's JWKS), issuer and expiry checked; the
    audience too when one is given. Raises jwt.PyJWTError on any failure —
    callers decide what that means for them."""
    signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        issuer=ZITADEL_ISSUER,
        audience=audience,
        options={"verify_aud": audience is not None},
    )


def verify_token(token: str) -> Claims:
    try:
        raw = decode_zitadel_jwt(token)
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"invalid token: {e}") from e

    if ORG_ID_CLAIM not in raw:
        raise HTTPException(status_code=401, detail="token missing org claim")
    return Claims(raw)


async def get_claims(authorization: str = Header(...)) -> Claims:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="expected 'Bearer <token>'")
    return verify_token(authorization.removeprefix("Bearer "))


def require_role(*allowed: str):
    """FR1 (04-api.md): mutating endpoints need an org-scoped RBAC check in
    shared middleware/dependency, not per-handler string comparisons."""

    async def _dep(claims: Claims = Depends(get_claims)) -> Claims:
        if not claims.roles & set(allowed):
            raise HTTPException(status_code=403, detail=f"requires one of roles: {allowed}")
        return claims

    return _dep


# The platform's own operators, not a tenant's users. req/02 FR3 fixes the
# org-scoped role set "at minimum"; this is the one role above it, held by
# whoever runs the platform, and it is what every /v1/admin route requires.
PLATFORM_ADMIN_ROLE = "platform_admin"


async def get_platform_admin(
    authorization: str | None = Header(default=None),
    x_admin_user: str | None = Header(default=None),
) -> str:
    """The actor behind a cross-tenant /v1/admin call.

    Same two-path shape as core.tenancy.get_tenant_id: a real Zitadel
    token when the caller has one — and it has to carry the
    platform_admin project role, since these routes read across every
    tenant — falling back to the X-Admin-User dev header when there is no
    Authorization header at all. The admin console signs in through
    app/modules/admin/auth and always sends the token; the header is left
    for curl.

    The token is either Zitadel's or one core-api issued itself to a
    password account (see issue_local_token below); the issuer says which,
    and each kind is then verified in full by its own rules.
    """
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ")
        if _is_local(token):
            return await _verify_local_token(token)
        claims = verify_token(token)
        if PLATFORM_ADMIN_ROLE not in claims.roles:
            raise HTTPException(status_code=403, detail=f"requires the {PLATFORM_ADMIN_ROLE} role")
        return claims.subject

    if not x_admin_user:
        raise HTTPException(
            status_code=401, detail="missing Authorization bearer token or X-Admin-User"
        )
    return x_admin_user


# --- The admin console's own password accounts ---------------------------
# admin_accounts (0032) is a way into the console that never touches
# Zitadel, so there is no Zitadel token to check: core-api signs its own,
# HS256 under ADMIN_LOCAL_TOKEN_SECRET. They are only ever accepted by
# get_platform_admin above — get_claims insists on RS256 from Zitadel's
# JWKS, so one of these opens no tenant-facing route.

LOCAL_ISSUER = "core-api/admin-accounts"
LOCAL_AUDIENCE = "admin-console"
LOCAL_SUBJECT_PREFIX = "local:"
# The console's session lasts as long as its token, so this is how long a
# password sign-in lasts — the same eight hours as a Zitadel one at most.
LOCAL_TOKEN_SECONDS = 8 * 60 * 60


def local_token_secret() -> str:
    # 32 is what RFC 7518 asks of an HS256 key; anything shorter is a
    # secret someone could guess offline from one token.
    if len(config.ADMIN_LOCAL_TOKEN_SECRET) < 32:
        raise HTTPException(
            status_code=503,
            detail="password sign-in is not configured: set ADMIN_LOCAL_TOKEN_SECRET to 32+ random characters",
        )
    return config.ADMIN_LOCAL_TOKEN_SECRET


def issue_local_token(username: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": LOCAL_ISSUER,
            "aud": LOCAL_AUDIENCE,
            "sub": LOCAL_SUBJECT_PREFIX + username,
            "iat": now,
            "exp": now + LOCAL_TOKEN_SECONDS,
        },
        local_token_secret(),
        algorithm="HS256",
    )


async def get_password_admin(authorization: str | None = Header(default=None)) -> str:
    """The username behind a password account's own token, for the routes
    that act on that account itself (app/modules/admin/auth/account.py).

    Narrower than get_platform_admin: a Zitadel operator has no row in
    admin_accounts to act on, and the X-Admin-User dev header is no proof
    of being anyone in particular, so neither gets past this.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing Authorization bearer token")
    token = authorization.removeprefix("Bearer ")
    if not _is_local(token):
        raise HTTPException(
            status_code=403,
            detail="only accounts that sign in with a username and password have a profile here",
        )
    return (await _verify_local_token(token)).removeprefix(LOCAL_SUBJECT_PREFIX)


def _is_local(token: str) -> bool:
    """Only routes the token to the right check — never trusted on its own,
    since anybody can write any issuer into an unsigned payload."""
    try:
        return jwt.decode(token, options={"verify_signature": False}).get("iss") == LOCAL_ISSUER
    except jwt.PyJWTError:
        return False


async def _verify_local_token(token: str) -> str:
    """The actor ("local:<username>"), once the signature holds and the
    account is still enabled and has not changed its password since.

    The account is read on every call: the token alone would keep a
    disabled operator in for the rest of their eight hours.
    """
    try:
        raw = jwt.decode(
            token,
            local_token_secret(),
            algorithms=["HS256"],
            issuer=LOCAL_ISSUER,
            audience=LOCAL_AUDIENCE,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"invalid token: {e}") from e

    username = raw["sub"].removeprefix(LOCAL_SUBJECT_PREFIX)
    async with (await db.get_pool()).acquire() as conn:
        account = await conn.fetchrow(
            "SELECT disabled, password_changed_at FROM admin_accounts WHERE lower(username) = lower($1)",
            username,
        )
    if account is None or account["disabled"]:
        raise HTTPException(status_code=401, detail="this account can no longer sign in")
    if raw["iat"] < int(account["password_changed_at"].timestamp()):
        raise HTTPException(status_code=401, detail="the password has changed since this sign-in")
    return raw["sub"]
