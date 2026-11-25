"""Key-authenticated routes (planning/04, 05): `require_key(Scope.PREDICT)`, a dependency family
separate from `require(Action)`. The key, not a person, is the principal, and it fixes the tenant and
project: X-Project-Id picks one of the key's granted projects, and a project the key was not granted is refused.

    missing, unknown, revoked or expired key, or X-Project-Id missing/not granted -> 401 invalid_api_key (one body)
    key without the scope                                                         -> 403 forbidden, naming the scope
    (the model allowlist is checked by the route, once it knows the model a call names)
    tenant not active (suspended or pending deletion)                             -> 503 tenant_suspended
"""
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from typing import NamedTuple

from fastapi import Header

from app.core.authz.deps import authorize
from app.core.authz.policy import Action, CallSource, Scope, TenantStatus
from app.core.db import get_pool
from app.core.problem import Problem, ProblemCode, not_found
from app.core.settings import settings
from app.core.tenancy import get_optional_claims, resolve_actor, resolve_tenant_id

KEY_PREFIX = "drl_"
DISPLAY_PREFIX_LEN = len(KEY_PREFIX) + 8


class NewKey(NamedTuple):
    secret: str  # shown once, never stored
    prefix: str
    key_hash: str


def hash_key(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def generate_key() -> NewKey:
    secret = KEY_PREFIX + secrets.token_urlsafe(32)
    return NewKey(secret, secret[:DISPLAY_PREFIX_LEN], hash_key(secret))


@dataclass(frozen=True, slots=True)
class KeyContext:
    key_id: uuid.UUID | None  # None for a signed-in member (source CONSOLE)
    tenant_id: str
    project_id: uuid.UUID
    scopes: frozenset[Scope]
    model_ids: frozenset[uuid.UUID]  # empty = every model in the project
    rate_limit_per_minute: int | None = None  # the key's own limit for POST /v1/predict; None = only the plan's
    source: CallSource = CallSource.API

    def allows_model(self, model_id: str | None) -> bool:
        if not self.model_ids:
            return True
        try:
            return uuid.UUID(model_id or "") in self.model_ids
        except ValueError:
            return False


def invalid_api_key() -> Problem:
    return Problem(HTTPStatus.UNAUTHORIZED, ProblemCode.INVALID_API_KEY, "missing or invalid API key")


def require_key(scope: Scope):
    """Route dependency: authenticate the key and check its scope. The model allowlist is the route's check (see KeyContext.allows_model)."""

    async def dependency(
        authorization: str | None = Header(default=None),
        x_project_id: str | None = Header(default=None),
    ) -> KeyContext:
        secret = (authorization or "").removeprefix("Bearer ")
        if not secret.startswith(KEY_PREFIX):
            raise invalid_api_key()
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM api_key_lookup($1)", hash_key(secret))
        if row is None:
            raise invalid_api_key()
        try:
            project_id = uuid.UUID(x_project_id or "")
        except ValueError:
            raise invalid_api_key() from None
        if project_id not in row["project_ids"]:  # a key works only in the projects granted to it
            raise invalid_api_key()
        ctx = KeyContext(
            row["key_id"], str(row["tenant_id"]), project_id,
            frozenset(Scope(s) for s in row["scopes"]), frozenset(row["model_ids"]), row["rate_limit_per_minute"],
        )
        if row["tenant_status"] != TenantStatus.ACTIVE:
            raise Problem(HTTPStatus.SERVICE_UNAVAILABLE, ProblemCode.TENANT_SUSPENDED, "this tenant is not active")
        if scope not in ctx.scopes:
            raise Problem(HTTPStatus.FORBIDDEN, ProblemCode.FORBIDDEN, f"missing scope {scope}", scope=scope.value)
        async with pool.acquire() as conn:
            await conn.execute("SELECT api_key_touch($1)", ctx.key_id)  # last_used_at, written at most once a minute
        return ctx

    dependency.authz_scope = scope  # read by tests/core/authz/test_matrix.py
    return dependency


def require_caller(scope: Scope, action: Action):
    """`require_key(scope)`, or a signed-in member of the console (planning/27 #8): a bearer that is not a `drl_` key is a user token,
    checked as `require(action)` would on the project named by X-Project-Id, and the call is tagged CONSOLE with no key behind it.
    Everything after authentication (limits, metering, storing) is the same for both."""
    key_dependency = require_key(scope)

    async def dependency(
        authorization: str | None = Header(default=None),
        x_project_id: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
    ) -> KeyContext:
        bearer = (authorization or "").removeprefix("Bearer ")
        member = (bearer and not bearer.startswith(KEY_PREFIX)) or (not authorization and settings.auth.dev_headers and x_tenant_id)
        if not member:
            return await key_dependency(authorization, x_project_id)
        claims = await get_optional_claims(authorization)
        tenant_id = await resolve_tenant_id(claims, x_tenant_id)
        actor = resolve_actor(claims, x_user_id)
        try:
            project_id = uuid.UUID(x_project_id or "")
        except ValueError:
            raise not_found() from None
        await authorize(action, tenant_id, actor, project_id)
        return KeyContext(None, tenant_id, project_id, frozenset({scope}), frozenset(), None, CallSource.CONSOLE)

    dependency.authz_scope = scope  # read by tests/core/authz/test_matrix.py
    return dependency
