"""Authorization (planning/04): enums and the role table in policy.py, the route dependency in deps.py.

    from app.core.authz import Action, AuthzContext, require
"""
from app.core.authz.deps import AuthzContext, public, require
from app.core.authz.keys import KeyContext, generate_key, require_key
from app.core.authz.policy import (
    ROLE_ACTIONS,
    Action,
    DefaultAccess,
    Role,
    Scope,
    TenantRole,
    TenantStatus,
    Visibility,
    allowed_actions,
)

__all__ = [
    "ROLE_ACTIONS", "Action", "AuthzContext", "DefaultAccess", "KeyContext", "Role", "Scope", "TenantRole",
    "TenantStatus", "Visibility", "allowed_actions", "generate_key", "public", "require", "require_key",
]
