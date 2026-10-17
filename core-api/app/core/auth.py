"""JWT verification against Zitadel (04-identity-tenancy-authz.md, ADR-021).

A token proves who someone is and nothing else: tenant membership and roles are Postgres rows
(core/tenancy.py). The one claim still read for authorization is the platform_admin project role
on `urn:zitadel:iam:org:project:roles` (a dict of role -> {org_id: org_name}), until /v1/admin
moves to a platform_admins table.
"""
import jwt
from fastapi import Header, HTTPException
from jwt import PyJWKClient
from urllib.parse import urlparse
from starlette.concurrency import run_in_threadpool

from app.core.settings import settings

ZITADEL_ISSUER = settings.zitadel.issuer
ROLES_CLAIM = "urn:zitadel:iam:org:project:roles"

# NFR2: cache JWKS, don't fetch on every request. PyJWKClient caches by
# default (lifespan=300s); one client for the process is enough.
_jwks_client: PyJWKClient | None = None


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        # Cloudflare in front of auth.drills.pro answers urllib's default User-Agent with 403.
        headers = {"User-Agent": "core-api"}
        base = settings.zitadel.internal_url
        if base:
            # Zitadel picks its instance from the host name, which is the public one.
            headers["Host"] = urlparse(ZITADEL_ISSUER).netloc
        _jwks_client = PyJWKClient(
            f"{base or ZITADEL_ISSUER}/oauth/v2/keys", cache_keys=True, headers=headers
        )
    return _jwks_client


def project_id() -> str:
    # Fail closed: without an audience to check, any token the instance signed would pass.
    pid = settings.zitadel.project_id
    if not pid:
        raise HTTPException(status_code=500, detail="ZITADEL_PROJECT_ID is not configured")
    return pid


class Claims:
    def __init__(self, raw: dict):
        self.raw = raw
        self.subject: str = raw["sub"]
        # Identity copy for the principals table; Zitadel only puts these on the access token
        # when the app/Action is configured to, so they may be absent.
        self.email: str | None = raw.get("email")
        self.email_verified: bool = raw.get("email_verified") is True
        self.display_name: str | None = raw.get("name")
        self.roles: set[str] = set(raw.get(ROLES_CLAIM, {}).keys())


def verify_token(token: str) -> Claims:
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        raw = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=ZITADEL_ISSUER,
            audience=project_id(),
            options={"require": ["exp", "iat", "sub", "aud"]},
        )
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"invalid token: {e}") from e
    return Claims(raw)


async def get_claims(authorization: str = Header(...)) -> Claims:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="expected 'Bearer <token>'")
    return await run_in_threadpool(verify_token, authorization.removeprefix("Bearer "))


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
    Authorization header at all, so curl and the admin dashboard keep
    working until that dashboard has a login of its own.
    """
    if authorization and authorization.startswith("Bearer "):
        claims = await run_in_threadpool(verify_token, authorization.removeprefix("Bearer "))
        if PLATFORM_ADMIN_ROLE not in claims.roles:
            raise HTTPException(status_code=403, detail=f"requires the {PLATFORM_ADMIN_ROLE} role")
        return claims.subject

    if not settings.auth.dev_headers or not x_admin_user:
        raise HTTPException(
            status_code=401, detail="missing Authorization bearer token or X-Admin-User"
        )
    return x_admin_user
