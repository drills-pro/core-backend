"""Who may do what: the closed sets and the one role -> actions table of planning/04.

Pure data and one pure function; nothing here touches the database or a request. Every enum is
mirrored by a CHECK constraint (tests/core/authz/test_policy.py fails when they differ).
"""
from collections.abc import Iterable
from enum import StrEnum


class TenantRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class TenantStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class Visibility(StrEnum):
    PRIVATE = "private"  # owner, admin and bound members only
    INTERNAL = "internal"  # every tenant member, with the project's default_access


class DefaultAccess(StrEnum):
    READ = "read"  # internal members act as viewer
    EDIT = "edit"  # internal members act as editor


class Role(StrEnum):
    VIEWER = "viewer"
    EDITOR = "editor"
    DATASET_EDITOR = "dataset_editor"
    MODEL_EDITOR = "model_editor"
    DEPLOYER = "deployer"
    PROJECT_ADMIN = "project_admin"


class Scope(StrEnum):
    """What an API key may do (mirrored by api_key_scopes_scope_check). Separate from Action: a key is
    not a person, and require_key checks a scope where require checks an action."""

    PREDICT = "predict"


class Action(StrEnum):
    # tenant-level
    TENANT_ADMIN = "tenant.admin"
    TENANT_MEMBER_MANAGE = "tenant.member.manage"
    BILLING_MANAGE = "billing.manage"
    PROJECT_CREATE = "project.create"
    PROJECT_LIST = "project.list"
    # project-scoped
    PROJECT_READ = "project.read"
    PROJECT_WRITE = "project.write"
    PROJECT_DELETE = "project.delete"
    PROJECT_MEMBER_MANAGE = "project.member.manage"
    AUDIT_READ = "audit.read"
    DATASET_READ = "dataset.read"
    DATASET_WRITE = "dataset.write"
    JOB_READ = "job.read"
    JOB_CREATE = "job.create"
    JOB_CANCEL = "job.cancel"
    MODEL_READ = "model.read"
    MODEL_WRITE = "model.write"
    MODEL_PROMOTE = "model.promote"
    DEPLOYMENT_READ = "deployment.read"
    DEPLOYMENT_TRY = "deployment.try"
    DEPLOYMENT_MANAGE = "deployment.manage"
    USAGE_READ = "usage.read"
    API_KEY_MANAGE = "api_key.manage"


A = Action
TENANT_ACTIONS = frozenset({A.TENANT_ADMIN, A.TENANT_MEMBER_MANAGE, A.BILLING_MANAGE, A.PROJECT_CREATE, A.PROJECT_LIST})
PROJECT_ACTIONS = frozenset(Action) - TENANT_ACTIONS

_VIEWER = frozenset({A.PROJECT_READ, A.DATASET_READ, A.JOB_READ, A.MODEL_READ, A.DEPLOYMENT_READ, A.USAGE_READ})
ROLE_ACTIONS: dict[Role, frozenset[Action]] = {
    Role.VIEWER: _VIEWER,
    Role.EDITOR: _VIEWER | {A.PROJECT_WRITE, A.DATASET_WRITE, A.JOB_CREATE, A.JOB_CANCEL, A.MODEL_WRITE, A.DEPLOYMENT_TRY},
    Role.DATASET_EDITOR: _VIEWER | {A.DATASET_WRITE},
    Role.MODEL_EDITOR: _VIEWER | {A.JOB_CREATE, A.JOB_CANCEL, A.MODEL_WRITE, A.MODEL_PROMOTE},
    Role.DEPLOYER: _VIEWER | {A.DEPLOYMENT_TRY, A.DEPLOYMENT_MANAGE, A.API_KEY_MANAGE},
    Role.PROJECT_ADMIN: PROJECT_ACTIONS,
}

# What `internal` grants every tenant member.
DEFAULT_ACCESS_ROLE = {DefaultAccess.READ: Role.VIEWER, DefaultAccess.EDIT: Role.EDITOR}

# Tenant-level actions per tenant role; owner and admin also hold every project action, implicitly.
_MEMBER_TENANT_ACTIONS = frozenset({A.PROJECT_CREATE, A.PROJECT_LIST})
MANAGER_ROLES = frozenset({TenantRole.OWNER, TenantRole.ADMIN})


def allowed_actions(
    tenant_role: TenantRole,
    visibility: Visibility | None,
    default_access: DefaultAccess | None,
    bound_roles: Iterable[Role],
) -> frozenset[Action]:
    """Everything the caller may do. Pass visibility None when there is no project in play.

    A member whose result holds no project action cannot see the project (private, unbound): the
    caller answers that with 404, not 403.
    """
    if tenant_role in MANAGER_ROLES:
        return frozenset(Action)
    actions = set(_MEMBER_TENANT_ACTIONS)
    if visibility is Visibility.INTERNAL:
        actions |= ROLE_ACTIONS[DEFAULT_ACCESS_ROLE[default_access]]
    for role in bound_roles:
        actions |= ROLE_ACTIONS[role]
    return frozenset(actions)
