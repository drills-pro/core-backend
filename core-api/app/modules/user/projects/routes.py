import uuid
from enum import StrEnum

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.core import audit
from app.core.authz import Action, AuthzContext, DefaultAccess, Role, Visibility, require
from app.core.enums import TaskType
from app.core.problem import Problem, not_found
from app.core.tenant_conn import TenantConn

router = APIRouter(prefix="/v1/projects", tags=["projects"])


class ProjectStatus(StrEnum):
    """Mirrors the projects.status CHECK constraint."""

    ACTIVE = "active"
    ARCHIVED = "archived"
    PENDING_DELETE = "pending_delete"


@router.get("")
async def list_projects(
    ctx: AuthzContext = Depends(require(Action.PROJECT_LIST)),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    # NFR1 (03-project-user-management.md): list endpoints must paginate.
    async with TenantConn(ctx.tenant_id) as conn:
        ids = await ctx.accessible_project_ids(conn)
        rows = await conn.fetch(
            "SELECT id, name, status, team_id, task_type, created_at FROM projects "
            "WHERE id = ANY($1) AND status != $2 ORDER BY created_at LIMIT $3 OFFSET $4",
            ids,
            ProjectStatus.PENDING_DELETE,
            limit,
            offset,
        )
    return [dict(r) for r in rows]


@router.post("", status_code=201)
async def create_project(
    name: str,
    task_type: TaskType,
    team_id: uuid.UUID | None = None,
    ctx: AuthzContext = Depends(require(Action.PROJECT_CREATE)),
):
    tenant_id, actor = ctx.tenant_id, ctx.actor
    async with TenantConn(tenant_id) as conn:
        if team_id is None:
            # FR1/FR2: every org has >=1 team (seeded at provisioning); a
            # project with no team_id given lands in the tenant's oldest
            # (Default) team rather than requiring every caller to look it up.
            team_id = await conn.fetchval(
                "SELECT id FROM teams ORDER BY created_at LIMIT 1"
            )
            if team_id is None:
                raise HTTPException(status_code=409, detail="tenant has no team; create one first")
        row = await conn.fetchrow(
            "INSERT INTO projects (tenant_id, name, team_id, task_type) VALUES ($1, $2, $3, $4) "
            "RETURNING id, name, status, team_id, task_type, created_at",
            tenant_id,
            name,
            team_id,
            task_type,
        )
        await audit.record(
            conn,
            tenant_id=tenant_id,
            project_id=row["id"],
            actor=actor,
            action="create",
            entity_type="project",
            entity_id=row["id"],
        )
        # The creator administers what they create (planning/04: auto-bound project_admin).
        await conn.execute(
            "INSERT INTO project_members (tenant_id, project_id, user_sub, role) VALUES ($1, $2, $3, $4)",
            tenant_id,
            row["id"],
            actor,
            Role.PROJECT_ADMIN,
        )
    return dict(row)


@router.get("/{project_id}")
async def get_project(project_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.PROJECT_READ))):
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT id, name, status, team_id, task_type, default_test_dataset_id, settings, "
            "visibility, default_access, publish_project_metadata, created_at "
            "FROM projects WHERE id = $1",
            project_id,
        )
    if row is None:
        raise not_found()  # RLS hides other tenants' rows: indistinguishable from a missing one
    return dict(row)


@router.patch("/{project_id}")
async def update_project(
    project_id: uuid.UUID,
    name: str | None = None,
    status: ProjectStatus | None = None,
    default_test_dataset_id: uuid.UUID | None = None,
    ctx: AuthzContext = Depends(require(Action.PROJECT_WRITE)),
):
    # FR1: rename/archive. `status` here covers archive/reactivate; delete
    # is its own endpoint below since it has different semantics (grace period).
    # default_test_dataset_id is FR5 (03-dataset-governance.md).
    if status is ProjectStatus.PENDING_DELETE:
        raise HTTPException(status_code=400, detail="status must be 'active' or 'archived'")
    if name is None and status is None and default_test_dataset_id is None:
        raise HTTPException(status_code=400, detail="nothing to update")

    async with TenantConn(ctx.tenant_id) as conn:
        if default_test_dataset_id is not None:
            owned = await conn.fetchval(
                "SELECT 1 FROM datasets WHERE id = $1 AND project_id = $2",
                default_test_dataset_id,
                project_id,
            )
            if not owned:
                raise not_found()
        row = await conn.fetchrow(
            "UPDATE projects SET name = COALESCE($2, name), status = COALESCE($3, status), "
            "default_test_dataset_id = COALESCE($4, default_test_dataset_id) "
            "WHERE id = $1 AND status != $5 "
            "RETURNING id, name, status, team_id, task_type, default_test_dataset_id, created_at",
            project_id,
            name,
            status,
            default_test_dataset_id,
            ProjectStatus.PENDING_DELETE,
        )
        if row is None:
            raise not_found()
        await audit.record(
            conn,
            tenant_id=ctx.tenant_id,
            project_id=project_id,
            actor=ctx.actor,
            action="update",
            entity_type="project",
            entity_id=project_id,
        )
    return dict(row)


_VALID_COMPUTE_TARGETS = ("cpu", "gpu")
_VALID_DEPLOY_STRATEGIES = ("main", "latest")


@router.patch("/{project_id}/settings")
async def update_project_settings(
    project_id: uuid.UUID,
    compute_target: str | None = Body(default=None),
    base_model: str | None = Body(default=None),
    deploy_strategy: str | None = Body(default=None),
    ctx: AuthzContext = Depends(require(Action.PROJECT_WRITE)),
):
    # FR1 (06-project-settings-deployment.md): merged into the existing
    # jsonb blob, not replaced — an omitted field keeps its current value.
    if compute_target is not None and compute_target not in _VALID_COMPUTE_TARGETS:
        raise HTTPException(status_code=400, detail=f"compute_target must be one of {_VALID_COMPUTE_TARGETS}")
    if deploy_strategy is not None and deploy_strategy not in _VALID_DEPLOY_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"deploy_strategy must be one of {_VALID_DEPLOY_STRATEGIES}")
    patch = {
        k: v
        for k, v in {
            "compute_target": compute_target,
            "base_model": base_model,
            "deploy_strategy": deploy_strategy,
        }.items()
        if v is not None
    }
    if not patch:
        raise HTTPException(status_code=400, detail="nothing to update")

    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "UPDATE projects SET settings = settings || $2::jsonb "
            "WHERE id = $1 AND status != $3 RETURNING id, settings",
            project_id,
            patch,
            ProjectStatus.PENDING_DELETE,
        )
        if row is None:
            raise not_found()
    return dict(row)


@router.patch("/{project_id}/visibility")
async def update_visibility(
    project_id: uuid.UUID,
    visibility: Visibility | None = None,
    default_access: DefaultAccess | None = None,
    ctx: AuthzContext = Depends(require(Action.PROJECT_MEMBER_MANAGE)),
):
    # FR4 (08-collaboration-visibility.md): who can see the project (visibility) and what `internal`
    # grants every tenant member (default_access, planning/04) are both project_admin decisions.
    if visibility is None and default_access is None:
        raise HTTPException(status_code=400, detail="nothing to update")
    async with TenantConn(ctx.tenant_id) as conn:
        # A gallery project must stay internal (CHECK): refuse rather than silently unpublish it.
        if visibility is Visibility.PRIVATE and await conn.fetchval(
            "SELECT gallery_published FROM projects WHERE id = $1", project_id
        ):
            raise Problem(409, "gallery_published", "unpublish the project from the gallery before making it private")
        row = await conn.fetchrow(
            "UPDATE projects SET visibility = COALESCE($2, visibility), "
            "default_access = COALESCE($3, default_access) "
            "WHERE id = $1 AND status != $4 "
            "RETURNING id, visibility, default_access",
            project_id,
            visibility,
            default_access,
            ProjectStatus.PENDING_DELETE,
        )
        if row is None:
            raise not_found()
    return dict(row)


@router.patch("/{project_id}/publish")
async def update_publish(
    project_id: uuid.UUID,
    publish_project_metadata: bool,
    ctx: AuthzContext = Depends(require(Action.PROJECT_WRITE)),
):
    # FR5 (08-collaboration-visibility.md): the metadata-publish flag is independent of visibility.
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "UPDATE projects SET publish_project_metadata = $2 WHERE id = $1 AND status != $3 "
            "RETURNING id, publish_project_metadata",
            project_id,
            publish_project_metadata,
            ProjectStatus.PENDING_DELETE,
        )
        if row is None:
            raise not_found()
    return dict(row)


@router.delete("/{project_id}", status_code=202)
async def delete_project(
    project_id: uuid.UUID,
    ctx: AuthzContext = Depends(require(Action.PROJECT_DELETE)),
):
    # FR1: delete is soft (30-day grace, req/01 FR2) — a background job
    # (not built yet, see req/01 open item) hard-deletes rows where
    # deleted_at < now() - interval '30 days'.
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "UPDATE projects SET status = $2, deleted_at = now() "
            "WHERE id = $1 AND status != $2 RETURNING id",
            project_id,
            ProjectStatus.PENDING_DELETE,
        )
        if row is None:
            raise not_found()
        await audit.record(
            conn,
            tenant_id=ctx.tenant_id,
            project_id=project_id,
            actor=ctx.actor,
            action="delete",
            entity_type="project",
            entity_id=project_id,
        )
    return {"id": str(project_id), "status": ProjectStatus.PENDING_DELETE}
