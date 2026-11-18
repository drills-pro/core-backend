"""Projects HTTP surface: parse, authorize, call the service, shape the response."""
import uuid

from fastapi import APIRouter, Depends

from app.core.authz import Action, AuthzContext, DefaultAccess, Visibility, require
from app.core.enums import TaskType
from app.core.pagination import Page, PageParams
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
    RoleSet,
    SharingChange,
    SharingResult,
    TrashedProject,
    VisibilityResult,
)

router = APIRouter(prefix="/v1/projects", tags=["projects"])
members_router = APIRouter(prefix="/v1/projects/{project_id}", tags=["project-members"])


@router.get("", response_model=Page[ProjectSummary])
async def list_projects(ctx: AuthzContext = Depends(require(Action.PROJECT_LIST)), page: PageParams = Depends()):
    return await service.list_projects(ctx, page)


# Before /{project_id}: "trash" would otherwise be parsed as an id.
@router.get("/trash", response_model=Page[TrashedProject])
async def list_trash(ctx: AuthzContext = Depends(require(Action.PROJECT_LIST)), page: PageParams = Depends()):
    return await service.list_trashed_projects(ctx, page)


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


@router.put("/{project_id}/sharing", response_model=SharingResult)
async def update_sharing(
    project_id: uuid.UUID,
    body: SharingChange,
    ctx: AuthzContext = Depends(require(Action.PROJECT_MEMBER_MANAGE)),
):
    return await service.update_sharing(ctx, project_id, body.sharing)


@router.delete("/{project_id}", status_code=202, response_model=DeleteResult)
async def delete_project(project_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.PROJECT_DELETE))):
    return await service.delete_project(ctx, project_id)


@router.post("/{project_id}:restore", response_model=ProjectSummary)
async def restore_project(project_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.PROJECT_DELETE))):
    return await service.restore_project(ctx, project_id)


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


@members_router.get("/audit-log", response_model=Page[AuditEntry])
async def get_audit_log(
    project_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.AUDIT_READ)), page: PageParams = Depends()
):
    return await service.audit_log(ctx, project_id, page)
