"""JWT verification against Zitadel (req/02-auth-identity.md FR1/FR3/NFR2).

Zitadel puts the org (=tenant, see req/01) on the `urn:zitadel:iam:user:resourceowner:id`
claim, and per-project roles on `urn:zitadel:iam:org:project:roles` (a dict of
role -> {org_id: org_name}). Both are stable, documented Zitadel claim keys,
not something to reverse-engineer per token.
"""
import jwt
from fastapi import Depends, Header, HTTPException
from jwt import PyJWKClient
from starlette.concurrency import run_in_threadpool

from app.core import config, vault

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


def project_id() -> str:
    # Fail closed: without an audience to check, any token the instance signed would pass.
    pid = config.ZITADEL_PROJECT_ID or vault.get_kv("zitadel-oidc").get("project_id", "")
    if not pid:
        raise HTTPException(status_code=500, detail="ZITADEL_PROJECT_ID is not configured")
    return pid


class Claims:
    def __init__(self, raw: dict):
        self.raw = raw
        self.subject: str = raw["sub"]
        self.org_id: str = raw[ORG_ID_CLAIM]
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

    if ORG_ID_CLAIM not in raw:
        raise HTTPException(status_code=401, detail="token missing org claim")
    return Claims(raw)


async def get_claims(authorization: str = Header(...)) -> Claims:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="expected 'Bearer <token>'")
    return await run_in_threadpool(verify_token, authorization.removeprefix("Bearer "))


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
    Authorization header at all, so curl and the admin dashboard keep
    working until that dashboard has a login of its own.
    """
    if authorization and authorization.startswith("Bearer "):
        claims = await run_in_threadpool(verify_token, authorization.removeprefix("Bearer "))
        if PLATFORM_ADMIN_ROLE not in claims.roles:
            raise HTTPException(status_code=403, detail=f"requires the {PLATFORM_ADMIN_ROLE} role")
        return claims.subject

    if not config.AUTH_DEV_HEADERS or not x_admin_user:
        raise HTTPException(
            status_code=401, detail="missing Authorization bearer token or X-Admin-User"
        )
    return x_admin_user
