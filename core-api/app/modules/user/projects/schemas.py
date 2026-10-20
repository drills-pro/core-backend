import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.authz import DefaultAccess, Role, Visibility
from app.core.enums import TaskType
from app.modules.user.projects.constants import ComputeTarget, DeployStrategy, ProjectStatus


class ProjectSummary(BaseModel):
    id: uuid.UUID
    name: str
    status: ProjectStatus
    team_id: uuid.UUID | None
    task_type: TaskType
    created_at: datetime


class ProjectUpdated(ProjectSummary):
    default_test_dataset_id: uuid.UUID | None


class ProjectDetail(ProjectUpdated):
    settings: dict[str, Any]
    visibility: Visibility
    default_access: DefaultAccess
    publish_project_metadata: bool


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


class PublishResult(BaseModel):
    id: uuid.UUID
    publish_project_metadata: bool


class DeleteResult(BaseModel):
    id: uuid.UUID
    status: ProjectStatus


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
