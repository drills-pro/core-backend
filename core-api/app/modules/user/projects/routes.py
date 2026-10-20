"""Projects HTTP surface: parse, authorize, call the service, shape the response."""
import uuid

from fastapi import APIRouter, Depends, Query

from app.core.authz import Action, AuthzContext, DefaultAccess, Visibility, require
from app.core.enums import TaskType
from app.modules.user.projects import service
from app.modules.user.projects.constants import ProjectStatus
from app.modules.user.projects.schemas import (
    AuditEntry,
    DeleteResult,
    Member,
    MemberRoles,
    ProjectDetail,
    ProjectSettings,
    ProjectSettingsPatch,
    ProjectSummary,
    ProjectUpdated,
    PublishResult,
    RoleSet,
    VisibilityResult,
)

router = APIRouter(prefix="/v1/projects", tags=["projects"])
members_router = APIRouter(prefix="/v1/projects/{project_id}", tags=["project-members"])


@router.get("", response_model=list[ProjectSummary])
async def list_projects(
    ctx: AuthzContext = Depends(require(Action.PROJECT_LIST)),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    return await service.list_projects(ctx, limit, offset)


@router.post("", status_code=201, response_model=ProjectSummary)
async def create_project(
    name: str,
    task_type: TaskType,
    team_id: uuid.UUID | None = None,
    ctx: AuthzContext = Depends(require(Action.PROJECT_CREATE)),
):
    return await service.create_project(ctx, name, task_type, team_id)


@router.get("/{project_id}", response_model=ProjectDetail)
async def get_project(project_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.PROJECT_READ))):
    return await service.get_project(ctx, project_id)


@router.patch("/{project_id}", response_model=ProjectUpdated)
async def update_project(
    project_id: uuid.UUID,
    name: str | None = None,
    status: ProjectStatus | None = None,
    default_test_dataset_id: uuid.UUID | None = None,
    ctx: AuthzContext = Depends(require(Action.PROJECT_WRITE)),
):
    return await service.update_project(ctx, project_id, name, status, default_test_dataset_id)


@router.patch("/{project_id}/settings", response_model=ProjectSettings)
async def update_project_settings(
    project_id: uuid.UUID, body: ProjectSettingsPatch, ctx: AuthzContext = Depends(require(Action.PROJECT_WRITE))
):
    return await service.update_settings(ctx, project_id, body)


@router.patch("/{project_id}/visibility", response_model=VisibilityResult)
async def update_visibility(
    project_id: uuid.UUID,
    visibility: Visibility | None = None,
    default_access: DefaultAccess | None = None,
    ctx: AuthzContext = Depends(require(Action.PROJECT_MEMBER_MANAGE)),
):
    return await service.update_visibility(ctx, project_id, visibility, default_access)


@router.patch("/{project_id}/publish", response_model=PublishResult)
async def update_publish(
    project_id: uuid.UUID,
    publish_project_metadata: bool,
    ctx: AuthzContext = Depends(require(Action.PROJECT_WRITE)),
):
    return await service.update_publish(ctx, project_id, publish_project_metadata)


@router.delete("/{project_id}", status_code=202, response_model=DeleteResult)
async def delete_project(project_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.PROJECT_DELETE))):
    return await service.delete_project(ctx, project_id)


@members_router.get("/members", response_model=list[Member])
async def list_members(project_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.PROJECT_READ))):
    return await service.list_members(ctx, project_id)


@members_router.put("/members/{user_sub}", response_model=MemberRoles)
async def set_member_roles(
    project_id: uuid.UUID,
    user_sub: str,
    body: RoleSet,
    ctx: AuthzContext = Depends(require(Action.PROJECT_MEMBER_MANAGE)),
):
    return await service.set_member_roles(ctx, project_id, user_sub, body.roles)


@members_router.delete("/members/{user_sub}", status_code=204)
async def remove_member(
    project_id: uuid.UUID, user_sub: str, ctx: AuthzContext = Depends(require(Action.PROJECT_MEMBER_MANAGE))
):
    await service.remove_member(ctx, project_id, user_sub)


@members_router.get("/audit-log", response_model=list[AuditEntry])
async def get_audit_log(
    project_id: uuid.UUID,
    ctx: AuthzContext = Depends(require(Action.AUDIT_READ)),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    return await service.audit_log(ctx, project_id, limit, offset)
