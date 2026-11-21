"""Projects, their settings, members and audit log."""
from app.integrations.data_plane import OwnedResource, register_ownership_check
from app.modules.user import tenant_members
from app.modules.user.projects.constants import ProjectStatus
from app.modules.user.projects.readers import (
    get_settings,
    get_task_type,
    get_team_id,
    owns_project,
    register_dataset_check,
)
from app.modules.user.projects.routes import members_router, router
from app.modules.user.projects.schemas import CreatedProject
from app.modules.user.projects.writers import create_project, remove_member_bindings

register_ownership_check(OwnedResource.PROJECT, owns_project)
tenant_members.register_member_removed(remove_member_bindings)

__all__ = [
    "CreatedProject",
    "ProjectStatus",
    "create_project",
    "get_settings",
    "get_task_type",
    "get_team_id",
    "members_router",
    "register_dataset_check",
    "router",
]
