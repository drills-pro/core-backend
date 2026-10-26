"""JWT verification against Zitadel (04-identity-tenancy-authz.md, ADR-021).

A token proves who someone is and nothing else: tenant membership and roles are Postgres rows
(core/tenancy.py). The one claim still read for authorization is the platform_admin project role
on `urn:zitadel:iam:org:project:roles` (a dict of role -> {org_id: org_name}), until /v1/admin
moves to a platform_admins table.
"""
import logging
import time
from urllib.parse import urlparse

import httpx
import jwt
from fastapi import Depends, Header, HTTPException
from jwt import PyJWKClient
from starlette.concurrency import run_in_threadpool

from app.core.metrics import auth as auth_metrics
from app.core.enums import AuthResult
from app.core.settings import settings

logger = logging.getLogger(__name__)

ZITADEL_ISSUER = settings.zitadel.issuer
ROLES_CLAIM = "urn:zitadel:iam:org:project:roles"

# NFR2: cache JWKS, don't fetch on every request. PyJWKClient caches by
# default (lifespan=300s); one client for the process is enough.
_jwks_client: PyJWKClient | None = None


def _zitadel_endpoint(path: str) -> tuple[str, dict[str, str]]:
    """URL and headers for a call to Zitadel: in-cluster when configured, else the public issuer."""
    # Cloudflare in front of auth.drills.pro answers urllib's default User-Agent with 403.
    headers = {"User-Agent": "core-api"}
    base = settings.zitadel.internal_url
    if base:
        # Zitadel picks its instance from the host name, which is the public one.
        headers["Host"] = urlparse(ZITADEL_ISSUER).netloc
    return f"{base or ZITADEL_ISSUER}{path}", headers


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        url, headers = _zitadel_endpoint("/oauth/v2/keys")
        _jwks_client = PyJWKClient(url, cache_keys=True, headers=headers)
    return _jwks_client


def project_id() -> str:
    # Fail closed: without an audience to check, any token the instance signed would pass.
    pid = settings.zitadel.project_id
    if not pid:
        raise HTTPException(status_code=500, detail="ZITADEL_PROJECT_ID is not configured")
    return pid


class Claims:
    def __init__(self, raw: dict, token: str = ""):
        self.raw = raw
        self.token = token
        self.subject: str = raw["sub"]
        # Identity copy for the principals table. An access token usually lacks these; see
        # get_profile_claims.
        self.email: str | None = raw.get("email")
        self.email_verified: bool = raw.get("email_verified") is True
        self.display_name: str | None = raw.get("name")
        self.roles: set[str] = set(raw.get(ROLES_CLAIM, {}).keys())


# Most specific first: InvalidSignatureError is a DecodeError and PyJWKClientConnectionError a
# PyJWKClientError, so the parent would otherwise match. Anything unlisted is MALFORMED.
_FAILURE_RESULTS = (
    (jwt.ExpiredSignatureError, AuthResult.EXPIRED),
    (jwt.InvalidAudienceError, AuthResult.WRONG_AUDIENCE),
    (jwt.InvalidIssuerError, AuthResult.WRONG_ISSUER),
    (jwt.InvalidSignatureError, AuthResult.BAD_SIGNATURE),
    (jwt.PyJWKClientConnectionError, AuthResult.JWKS_UNAVAILABLE),
    (jwt.PyJWKClientError, AuthResult.UNKNOWN_KEY),
)


def _count_verification(result: AuthResult) -> None:
    auth_metrics.token_verifications.add(1, {"result": result})


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
        _count_verification(next((r for cls, r in _FAILURE_RESULTS if isinstance(e, cls)), AuthResult.MALFORMED))
        raise HTTPException(status_code=401, detail=f"invalid token: {e}") from e
    _count_verification(AuthResult.OK)
    return Claims(raw, token)


async def get_claims(authorization: str = Header(...)) -> Claims:
    if not authorization.startswith("Bearer "):
        _count_verification(AuthResult.NOT_BEARER)
        raise HTTPException(status_code=401, detail="expected 'Bearer <token>'")
    return await run_in_threadpool(verify_token, authorization.removeprefix("Bearer "))


_USERINFO_TTL = 300
_userinfo_cache: dict[str, tuple[float, dict]] = {}  # token -> (expires at, userinfo)


async def get_profile_claims(claims: Claims = Depends(get_claims)) -> Claims:
    """Claims with email and name filled in from Zitadel's userinfo when the access token has none
    (it does not, unless an Action adds them). Only the routes that need a profile use this, so
    ordinary requests make no extra call. If userinfo is unreachable the claims stay as they are."""
    if claims.email is not None:
        return claims
    now = time.monotonic()
    hit = _userinfo_cache.get(claims.token)
    if hit is None or hit[0] < now:
        url, headers = _zitadel_endpoint("/oidc/v1/userinfo")
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(url, headers={**headers, "Authorization": f"Bearer {claims.token}"})
            resp.raise_for_status()
        except (httpx.HTTPError, OSError):
            logger.warning("zitadel userinfo failed for sub %s", claims.subject, exc_info=True)
            metrics.zitadel_userinfo_total.add(1, {"result": UserinfoResult.FAILED})
            return claims
        if len(_userinfo_cache) > 1000:
            _userinfo_cache.clear()  # ponytail: crude bound; an LRU if this ever matters
        hit = _userinfo_cache[claims.token] = (now + _USERINFO_TTL, resp.json())
        metrics.zitadel_userinfo_total.add(1, {"result": UserinfoResult.FETCHED})
    else:
        metrics.zitadel_userinfo_total.add(1, {"result": UserinfoResult.CACHED})
    info = hit[1]
    claims.email = info.get("email")
    claims.email_verified = info.get("email_verified") is True
    claims.display_name = info.get("name")
    return claims


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
