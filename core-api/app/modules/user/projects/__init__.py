"""Projects, their settings, members and audit log."""
from app.integrations.data_plane import access
from app.modules.user.projects.readers import (
    get_settings,
    get_task_type,
    get_team_id,
    owns_project,
    register_dataset_check,
)
from app.modules.user.projects.routes import members_router, router
from app.modules.user.projects.writers import create_project

access.register(access.Resource.PROJECT, owns_project)

__all__ = [
    "create_project",
    "get_settings",
    "get_task_type",
    "get_team_id",
    "members_router",
    "register_dataset_check",
    "router",
]
