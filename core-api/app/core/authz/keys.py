"""Key-authenticated routes (planning/04, 05): `require_key(Scope.PREDICT)`, a dependency family
separate from `require(Action)`. The key, not a person, is the principal, and it fixes the tenant and
project: the X-Project-Id header may only confirm the key's project, never choose one.

    missing, unknown, revoked or expired key, or X-Project-Id missing/different  -> 401 invalid_api_key (one body)
    key without the scope                                                         -> 403 forbidden, naming the scope
    model outside a non-empty allowlist                                           -> 403 forbidden
"""
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from typing import NamedTuple

from fastapi import Header

from app.core.authz.policy import Scope
from app.core.db import get_pool
from app.core.problem import Problem

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
    key_id: uuid.UUID
    tenant_id: str
    project_id: uuid.UUID
    scopes: frozenset[Scope]
    model_ids: frozenset[uuid.UUID]  # empty = every model in the project

    def allows_model(self, model_id: str | None) -> bool:
        if not self.model_ids:
            return True
        try:
            return uuid.UUID(model_id or "") in self.model_ids
        except ValueError:
            return False


def invalid_api_key() -> Problem:
    return Problem(401, "invalid_api_key", "missing or invalid API key")


def require_key(scope: Scope):
    """Route dependency: authenticate the key, check its scope and (when X-Model-Id is sent) its model allowlist."""

    async def dependency(
        authorization: str | None = Header(default=None),
        x_project_id: str | None = Header(default=None),
        x_model_id: str | None = Header(default=None),
    ) -> KeyContext:
        secret = (authorization or "").removeprefix("Bearer ")
        if not secret.startswith(KEY_PREFIX):
            raise invalid_api_key()
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM api_key_lookup($1)", hash_key(secret))
        if row is None or x_project_id != str(row["project_id"]):
            raise invalid_api_key()
        ctx = KeyContext(
            row["key_id"], str(row["tenant_id"]), row["project_id"],
            frozenset(Scope(s) for s in row["scopes"]), frozenset(row["model_ids"]),
        )
        if scope not in ctx.scopes:
            raise Problem(403, "forbidden", f"missing scope {scope}", scope=scope.value)
        if not ctx.allows_model(x_model_id):
            raise Problem(403, "forbidden", "this key may not use that model")
        return ctx

    dependency.authz_scope = scope  # read by tests/core/authz/test_matrix.py
    return dependency
