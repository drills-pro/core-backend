import uuid
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field, computed_field

from app.core.authz import DefaultAccess, Role, Visibility
from app.core.enums import TaskType
from app.integrations.serve_client import ComputeTarget
from app.modules.user.projects.constants import PROJECT_RETENTION_DAYS, DeployStrategy, ProjectStatus, Sharing


class ProjectSummary(BaseModel):
    id: uuid.UUID
    name: str
    status: ProjectStatus
    team_id: uuid.UUID | None
    task_type: TaskType
    created_at: datetime


class ProjectListItem(ProjectSummary):
    """A row of GET /v1/projects: the summary plus what the project card counts (live rows only)."""

    datasets: int
    models: int
    deployments: int


class CreatedProject(ProjectSummary):
    visibility: Visibility
    default_access: DefaultAccess
    gallery_published: bool


class ProjectUpdated(ProjectSummary):
    default_test_dataset_id: uuid.UUID | None


class ProjectDetail(ProjectUpdated):
    settings: dict[str, Any]
    visibility: Visibility
    default_access: DefaultAccess
    sharing: Sharing


class ProjectSettingsPatch(BaseModel):
    """Merged into the stored settings; an omitted field keeps its current value."""

    compute_target: ComputeTarget | None = None
    base_model: str | None = None
    deploy_strategy: DeployStrategy | None = None


class ProjectSettings(BaseModel):
    id: uuid.UUID
    settings: dict[str, Any]


class VisibilityResult(BaseModel):
    id: uuid.UUID
    visibility: Visibility
    default_access: DefaultAccess
    sharing: Sharing  # a private project is also out of the gallery


class SharingChange(BaseModel):
    sharing: Sharing


class SharingResult(BaseModel):
    id: uuid.UUID
    sharing: Sharing
    visibility: Visibility  # a gallery level makes the project internal


class DeleteResult(BaseModel):
    id: uuid.UUID
    status: ProjectStatus
    deleted_at: datetime

    @computed_field
    @property
    def restorable_until(self) -> datetime:
        return self.deleted_at + timedelta(days=PROJECT_RETENTION_DAYS)


class TrashedProject(BaseModel):
    """A project in the Trash: restorable until `restorable_until`."""

    id: uuid.UUID
    name: str
    deleted_at: datetime

    @computed_field
    @property
    def restorable_until(self) -> datetime:
        return self.deleted_at + timedelta(days=PROJECT_RETENTION_DAYS)


class RoleSet(BaseModel):
    roles: list[Role] = Field(min_length=1)


class Member(BaseModel):
    user_sub: str
    roles: list[Role]
    created_at: datetime


class MemberRoles(BaseModel):
    project_id: uuid.UUID
    user_sub: str
    roles: list[Role]


class AuditEntry(BaseModel):
    id: uuid.UUID
    actor: str
    action: str  # free text: older rows hold actions the enum no longer lists
    entity_type: str
    entity_id: str
    created_at: datetime
