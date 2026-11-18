"""Authorization (planning/04): enums and the role table in policy.py, the route dependency in deps.py.

    from app.core.authz import Action, AuthzContext, require
"""
from app.core.authz.deps import AuthzContext, authorize, public, require
from app.core.authz.keys import KeyContext, generate_key, require_caller, require_key
from app.core.authz.policy import (
    ROLE_ACTIONS,
    Action,
    CallSource,
    DefaultAccess,
    InviteRole,
    Role,
    Scope,
    TenantRole,
    TenantStatus,
    Visibility,
    allowed_actions,
)

__all__ = [
    "ROLE_ACTIONS", "Action", "AuthzContext", "CallSource", "DefaultAccess", "InviteRole", "KeyContext", "Role", "Scope", "TenantRole",
    "TenantStatus", "Visibility", "allowed_actions", "authorize", "generate_key", "public", "require", "require_caller", "require_key",
]
