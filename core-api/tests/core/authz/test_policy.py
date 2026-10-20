"""The role -> actions table of planning/04, restated independently, and the pure policy function."""
import re
from pathlib import Path

import pytest

from app.core.authz import ROLE_ACTIONS, Action, DefaultAccess, InviteRole, Role, Scope, TenantRole, TenantStatus, Visibility, allowed_actions
from app.core.enums import TaskType

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
MIGRATIONS = "\n".join(  # the up part of each file, in order: the last definition wins
    p.read_text().split("-- migrate:down")[0] for p in sorted((Path(__file__).parents[3] / "migrations").glob("*.sql"))
)


def _check_values(constraint: str) -> set[str]:
    """The IN (...) list of the last definition of a named CHECK constraint (or a column's inline one)."""
    found = re.findall(rf"{constraint}\s+CHECK \(\w+ IN \(([^)]*)\)\)", MIGRATIONS)
    assert found, f"no CHECK named {constraint} in the migrations"
    return set(re.findall(r"'([^']+)'", found[-1]))


@pytest.mark.parametrize(
    ("enum", "constraint"),
    [
        (Role, "CONSTRAINT project_members_role_check"),
        (Scope, "CONSTRAINT api_key_scopes_scope_check"),
        (Visibility, "CONSTRAINT projects_visibility_check"),
        (DefaultAccess, "CONSTRAINT projects_default_access_check"),
        (TenantStatus, "CONSTRAINT tenants_status_check"),
        (InviteRole, "CONSTRAINT tenant_invitations_role_check"),
        (TaskType, r"task_type\s+text"),  # projects.task_type, inline in init
        (TenantRole, r"role\s+text NOT NULL"),  # tenant_members.role, inline in the shared-pool migration
    ],
)
def test_enum_matches_its_check_constraint(enum, constraint):
    assert {m.value for m in enum} == _check_values(constraint)
