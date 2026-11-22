"""The role -> actions table of planning/04, restated independently, and the pure policy function."""
import pytest

from app.core.authz import ROLE_ACTIONS, Action, DefaultAccess, InviteRole, Role, Scope, TenantRole, TenantStatus, Visibility, allowed_actions
from app.core.enums import TaskType
from app.modules.user.datasets.constants import ImportSource, ImportStatus
from app.modules.user.deployments.status import DeploymentStatus
from app.modules.user.training.constants import TrainingStatus
from app.modules.user.usage.constants import UsageResource
from tests.support.check_mirror import assert_mirrors

VIEWER = {"project.read", "dataset.read", "job.read", "model.read", "deployment.read", "usage.read"}
TABLE = {
    Role.VIEWER: VIEWER,
    Role.EDITOR: VIEWER | {"project.write", "dataset.write", "job.create", "job.cancel", "model.write", "deployment.try"},
    Role.DATASET_EDITOR: VIEWER | {"dataset.write"},
    Role.MODEL_EDITOR: VIEWER | {"job.create", "job.cancel", "model.write", "model.promote"},
    Role.DEPLOYER: VIEWER | {"deployment.try", "deployment.manage", "api_key.manage"},
    Role.PROJECT_ADMIN: VIEWER | {
        "project.write", "project.delete", "project.member.manage", "audit.read", "dataset.write", "job.create",
        "job.cancel", "model.write", "model.promote", "deployment.try", "deployment.manage", "api_key.manage",
    },
}
TENANT_MEMBER = {Action.PROJECT_CREATE, Action.PROJECT_LIST}
INTERNAL, PRIVATE = Visibility.INTERNAL, Visibility.PRIVATE
READ, EDIT = DefaultAccess.READ, DefaultAccess.EDIT


@pytest.mark.parametrize("role", Role)
def test_role_table(role):
    assert {a.value for a in ROLE_ACTIONS[role]} == TABLE[role]


def member(visibility, default_access, *roles):
    return allowed_actions(TenantRole.MEMBER, visibility, default_access, roles)


@pytest.mark.parametrize("visibility", [None, PRIVATE, INTERNAL])
def test_owner_holds_everything_and_admin_all_but_the_owner_only_actions(visibility):
    assert allowed_actions(TenantRole.OWNER, visibility, READ, []) == frozenset(Action)
    assert allowed_actions(TenantRole.ADMIN, visibility, READ, []) == frozenset(Action) - {Action.TENANT_DELETE, Action.TENANT_TRANSFER_OWNERSHIP}


def test_member_holds_only_create_and_list_outside_a_project():
    assert member(None, None) == TENANT_MEMBER


def test_private_without_binding_shows_no_project_action():
    assert member(PRIVATE, READ) == TENANT_MEMBER


def test_internal_read_is_viewer_and_internal_edit_is_editor():
    assert member(INTERNAL, READ) == ROLE_ACTIONS[Role.VIEWER] | TENANT_MEMBER
    assert member(INTERNAL, EDIT) == ROLE_ACTIONS[Role.EDITOR] | TENANT_MEMBER


def test_bindings_add_on_top_of_internal_and_never_remove():
    assert member(INTERNAL, READ, Role.DEPLOYER) >= member(INTERNAL, READ)
    assert Action.DEPLOYMENT_MANAGE in member(INTERNAL, READ, Role.DEPLOYER)
    assert Action.DEPLOYMENT_MANAGE not in member(INTERNAL, READ)
    assert member(INTERNAL, EDIT, Role.VIEWER) == member(INTERNAL, EDIT)


def test_private_bound_roles_are_a_union():
    got = member(PRIVATE, READ, Role.DATASET_EDITOR, Role.MODEL_EDITOR)
    assert got == ROLE_ACTIONS[Role.DATASET_EDITOR] | ROLE_ACTIONS[Role.MODEL_EDITOR] | TENANT_MEMBER
    assert Action.DEPLOYMENT_MANAGE not in got


def test_tenant_level_actions_are_not_granted_by_project_roles():
    got = member(INTERNAL, EDIT, Role.PROJECT_ADMIN)
    assert not got & {Action.TENANT_ADMIN, Action.TENANT_MEMBER_MANAGE, Action.BILLING_MANAGE, Action.TENANT_DELETE}


# Drift: each enum equals the CHECK constraint that mirrors it, read from the migration files.
@pytest.mark.parametrize(
    ("enum", "table", "column"),
    [
        (Role, "project_members", "role"),
        (Scope, "api_key_scopes", "scope"),
        (Visibility, "projects", "visibility"),
        (DefaultAccess, "projects", "default_access"),
        (TenantStatus, "tenants", "status"),
        (InviteRole, "tenant_invitations", "role"),
        (TaskType, "projects", "task_type"),
        (ImportStatus, "dataset_imports", "status"),
        (ImportSource, "dataset_imports", "source"),
        (DeploymentStatus, "deployments", "status"),
        (TrainingStatus, "training_runs", "status"),
        (UsageResource, "usage_events", "resource_type"),
        (TenantRole, "tenant_members", "role"),
    ],
)
def test_enum_matches_its_check_constraint(enum, table, column):
    assert_mirrors(table, column, enum)
