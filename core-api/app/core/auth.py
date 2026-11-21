"""JWT verification against Zitadel (04-identity-tenancy-authz.md, ADR-021).

A token proves who someone is and nothing else: tenant membership and roles are Postgres rows
(core/tenancy.py). The one claim still read for authorization is the platform_admin project role
on `urn:zitadel:iam:org:project:roles` (a dict of role -> {org_id: org_name}), until /v1/admin
moves to a platform_admins table.
"""
import logging
import time
from http import HTTPStatus
from urllib.parse import urlparse

import httpx
import jwt
from fastapi import Depends, Header
from jwt import PyJWKClient
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from starlette.concurrency import run_in_threadpool

from app.core import auth_log
from app.core.metrics import auth as auth_metrics
from app.core.enums import AuthAttribute, AuthResult, AuthSpan, UserinfoResult
from app.core.problem import Problem, ProblemCode
from app.core.settings import settings
from app.core.tracing import tag_current_span, tracer

logger = logging.getLogger(__name__)

ZITADEL_ISSUER = settings.zitadel.issuer
ROLES_CLAIM = "urn:zitadel:iam:org:project:roles"
PROJECT_ROLES_CLAIM = "urn:zitadel:iam:org:project:{project_id}:roles"  # Zitadel's claim when the token asks for the project's audience

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


class _TracedJWKClient(PyJWKClient):
    """PyJWKClient fetches with urllib, which no instrumentor sees; this gives each fetch (a cache miss) a span."""

    def fetch_data(self):
        with tracer.start_as_current_span(AuthSpan.JWKS_FETCH):
            return super().fetch_data()


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        url, headers = _zitadel_endpoint("/oauth/v2/keys")
        _jwks_client = _TracedJWKClient(url, cache_keys=True, headers=headers)
    return _jwks_client


def project_id() -> str:
    # Fail closed: without an audience to check, any token the instance signed would pass.
    pid = settings.zitadel.project_id
    if not pid:
        raise Problem(HTTPStatus.INTERNAL_SERVER_ERROR, ProblemCode.AUTH_NOT_CONFIGURED, "ZITADEL_PROJECT_ID is not configured")
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
        project_roles = raw.get(PROJECT_ROLES_CLAIM.format(project_id=settings.zitadel.project_id), {})
        self.roles: set[str] = set(raw.get(ROLES_CLAIM, {})) | set(project_roles)


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


def _verify_span():
    # Status is set from the AuthResult, not from the exception, whose text could quote the token.
    return tracer.start_as_current_span(AuthSpan.VERIFY_TOKEN, record_exception=False, set_status_on_exception=False)


def _count_verification(result: AuthResult) -> None:
    """Record a result on the metric, the verify span and, for a rejection, the auth log."""
    span = trace.get_current_span()
    span.set_attribute(AuthAttribute.RESULT, result)
    auth_metrics.token_verifications.add(1, {"result": result})
    if result is not AuthResult.OK:
        span.set_status(Status(StatusCode.ERROR, result))
        auth_log.token_rejected(result)


def verify_token(token: str) -> Claims:
    with _verify_span() as span:
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
            raise Problem(HTTPStatus.UNAUTHORIZED, ProblemCode.INVALID_TOKEN, f"invalid token: {e}") from e
        _count_verification(AuthResult.OK)
        claims = Claims(raw, token)
        span.set_attribute(AuthAttribute.USER, claims.subject)
    # Every authenticated path comes through here, so the request span gets the user too: Tempo can
    # then find all of one user's requests, not only their verify spans.
    tag_current_span(**{AuthAttribute.USER: claims.subject})
    return claims


async def get_claims(authorization: str | None = Header(None)) -> Claims:
    if authorization is None or not authorization.startswith("Bearer "):  # no header is 401 too, not FastAPI's 422
        with _verify_span():
            _count_verification(AuthResult.NOT_BEARER)
        raise Problem(HTTPStatus.UNAUTHORIZED, ProblemCode.BEARER_REQUIRED, "expected 'Bearer <token>'")
    return await run_in_threadpool(verify_token, authorization.removeprefix("Bearer "))


_USERINFO_TTL = 300
_userinfo_cache: dict[str, tuple[float, dict]] = {}  # token -> (expires at, userinfo)


async def _userinfo(claims: Claims) -> tuple[UserinfoResult, dict | None]:
    now = time.monotonic()
    hit = _userinfo_cache.get(claims.token)
    if hit is not None and hit[0] >= now:
        return UserinfoResult.CACHED, hit[1]
    url, headers = _zitadel_endpoint("/oidc/v1/userinfo")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url, headers={**headers, "Authorization": f"Bearer {claims.token}"})
        resp.raise_for_status()
    except (httpx.HTTPError, OSError):
        logger.warning("zitadel userinfo failed for sub %s", claims.subject, exc_info=True)
        return UserinfoResult.FAILED, None
    if len(_userinfo_cache) > 1000:
        _userinfo_cache.clear()  # ponytail: crude bound; an LRU if this ever matters
    info = resp.json()
    _userinfo_cache[claims.token] = (now + _USERINFO_TTL, info)
    return UserinfoResult.FETCHED, info


async def get_profile_claims(claims: Claims = Depends(get_claims)) -> Claims:
    """Claims with email and name filled in from Zitadel's userinfo when the access token has none
    (it does not, unless an Action adds them). Only the routes that need a profile use this, so
    ordinary requests make no extra call. If userinfo is unreachable the claims stay as they are."""
    if claims.email is not None:
        return claims
    with tracer.start_as_current_span(AuthSpan.USERINFO) as span:
        result, info = await _userinfo(claims)
        span.set_attribute(AuthAttribute.USERINFO_RESULT, result)
        if result is UserinfoResult.FAILED:
            span.set_status(Status(StatusCode.ERROR, result))
    auth_metrics.zitadel_userinfo.add(1, {"result": result})
    if info is None:
        return claims
    claims.email = info.get("email")
    claims.email_verified = info.get("email_verified") is True
    claims.display_name = info.get("name")
    return claims


def dev_header(value: str | None, header: str, *, default: str | None = None) -> str:
    """The caller named by a dev header, for a request with no bearer token: the one place the dev-header rule lives. Refused
    (401) unless `auth.dev_headers` is on (never in prod) and the header, or `default`, gives a value."""
    if not settings.auth.dev_headers or not (value or default):
        raise Problem(HTTPStatus.UNAUTHORIZED, ProblemCode.UNAUTHENTICATED, f"missing Authorization bearer token or {header}")
    return value or default


# The platform's own operators, not a tenant's users. req/02 FR3 fixes the
# org-scoped role set "at minimum"; this is the one role above it, held by
# whoever runs the platform, and it is what every /v1/admin route requires.
PLATFORM_ADMIN_ROLE = "platform_admin"
ADMIN_USER_HEADER = "X-Admin-User"


async def get_platform_admin(
    authorization: str | None = Header(default=None),
    x_admin_user: str | None = Header(default=None),
) -> str:
    """The actor behind a cross-tenant /v1/admin call.

    The same two paths as core.tenancy.get_tenant_id: a real Zitadel
    token when the caller has one — and it has to carry the
    platform_admin project role, since these routes read across every
    tenant — falling back to the X-Admin-User dev header when there is no
    Authorization header at all, so curl and the admin dashboard keep
    working until that dashboard has a login of its own.
    """
    if authorization and authorization.startswith("Bearer "):
        claims = await run_in_threadpool(verify_token, authorization.removeprefix("Bearer "))
        if PLATFORM_ADMIN_ROLE not in claims.roles:
            raise Problem(HTTPStatus.FORBIDDEN, ProblemCode.PLATFORM_ADMIN_REQUIRED, f"requires the {PLATFORM_ADMIN_ROLE} role")
        return claims.subject

    return dev_header(x_admin_user, ADMIN_USER_HEADER)
