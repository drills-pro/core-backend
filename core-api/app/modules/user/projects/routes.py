import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.core import audit
from app.core.tenancy import get_actor, get_tenant_id
from app.core.tenant_conn import TenantConn

router = APIRouter(prefix="/v1/projects", tags=["projects"])

# FR4 (02-org-team-project-model.md): task type is set at creation, one of
# a fixed vocabulary, or the legacy escape hatch for pre-Phase-2 data.
VALID_TASK_TYPES = (
    "image_classification",
    "object_detection",
    "tabular_classification",
    "text_classification",
    "legacy_untyped",
)


@router.get("")
async def list_projects(
    tenant_id: str = Depends(get_tenant_id),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    # NFR1 (03-project-user-management.md): list endpoints must paginate.
    async with TenantConn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT id, name, status, team_id, task_type, created_at FROM projects "
            "WHERE status != 'pending_delete' ORDER BY created_at LIMIT $1 OFFSET $2",
            limit,
            offset,
        )
    return [dict(r) for r in rows]


@router.post("", status_code=201)
async def create_project(
    name: str,
    task_type: str,
    team_id: uuid.UUID | None = None,
    tenant_id: str = Depends(get_tenant_id),
    actor: str = Depends(get_actor),
):
    if task_type not in VALID_TASK_TYPES:
        raise HTTPException(status_code=400, detail=f"task_type must be one of {VALID_TASK_TYPES}")
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
    return dict(row)


@router.get("/{project_id}")
async def get_project(project_id: uuid.UUID, tenant_id: str = Depends(get_tenant_id)):
    async with TenantConn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT id, name, status, team_id, task_type, default_test_dataset_id, settings, "
            "visibility, publish_project_metadata, created_at "
            "FROM projects WHERE id = $1",
            project_id,
        )
        if row:
            return dict(row)

        # RLS hid the row. Per req/01 FR1: distinguish "doesn't exist" (404)
        # from "exists in a tenant you can't see" (403) without leaking row
        # contents — project_exists_any_tenant runs SECURITY DEFINER and
        # bypasses RLS just for the boolean existence check.
        exists = await conn.fetchval("SELECT project_exists_any_tenant($1)", project_id)
    if exists:
        raise HTTPException(status_code=403, detail="not accessible in this tenant")
    raise HTTPException(status_code=404, detail="project not found")


@router.patch("/{project_id}")
async def update_project(
    project_id: uuid.UUID,
    name: str | None = None,
    status: str | None = None,
    default_test_dataset_id: uuid.UUID | None = None,
    tenant_id: str = Depends(get_tenant_id),
    actor: str = Depends(get_actor),
):
    # FR1: rename/archive. `status` here covers archive/reactivate; delete
    # is its own endpoint below since it has different semantics (grace period).
    # default_test_dataset_id is FR5 (03-dataset-governance.md).
    if status is not None and status not in ("active", "archived"):
        raise HTTPException(status_code=400, detail="status must be 'active' or 'archived'")
    if name is None and status is None and default_test_dataset_id is None:
        raise HTTPException(status_code=400, detail="nothing to update")

    async with TenantConn(tenant_id) as conn:
        if default_test_dataset_id is not None:
            owned = await conn.fetchval(
                "SELECT 1 FROM datasets WHERE id = $1 AND project_id = $2",
                default_test_dataset_id,
                project_id,
            )
            if not owned:
                raise HTTPException(status_code=404, detail="dataset not found in this project")
        row = await conn.fetchrow(
            "UPDATE projects SET name = COALESCE($2, name), status = COALESCE($3, status), "
            "default_test_dataset_id = COALESCE($4, default_test_dataset_id) "
            "WHERE id = $1 AND status != 'pending_delete' "
            "RETURNING id, name, status, team_id, task_type, default_test_dataset_id, created_at",
            project_id,
            name,
            status,
            default_test_dataset_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
        await audit.record(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            actor=actor,
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
    tenant_id: str = Depends(get_tenant_id),
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

    async with TenantConn(tenant_id) as conn:
        row = await conn.fetchrow(
            "UPDATE projects SET settings = settings || $2::jsonb "
            "WHERE id = $1 AND status != 'pending_delete' RETURNING id, settings",
            project_id,
            patch,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
    return dict(row)


@router.patch("/{project_id}/visibility")
async def update_visibility(
    project_id: uuid.UUID,
    visibility: str | None = None,
    publish_project_metadata: bool | None = None,
    tenant_id: str = Depends(get_tenant_id),
):
    # FR4/FR5 (08-collaboration-visibility.md): visibility and the
    # metadata-publish flag are independent — a project can be public with
    # metadata still hidden ("a public project exists" with no name shown).
    if visibility is not None and visibility not in ("private", "public"):
        raise HTTPException(status_code=400, detail="visibility must be 'private' or 'public'")
    if visibility is None and publish_project_metadata is None:
        raise HTTPException(status_code=400, detail="nothing to update")
    async with TenantConn(tenant_id) as conn:
        row = await conn.fetchrow(
            "UPDATE projects SET visibility = COALESCE($2, visibility), "
            "publish_project_metadata = COALESCE($3, publish_project_metadata) "
            "WHERE id = $1 AND status != 'pending_delete' "
            "RETURNING id, visibility, publish_project_metadata",
            project_id,
            visibility,
            publish_project_metadata,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
    return dict(row)


@router.delete("/{project_id}", status_code=202)
async def delete_project(
    project_id: uuid.UUID,
    tenant_id: str = Depends(get_tenant_id),
    actor: str = Depends(get_actor),
):
    # FR1: delete is soft (30-day grace, req/01 FR2) — a background job
    # (not built yet, see req/01 open item) hard-deletes rows where
    # deleted_at < now() - interval '30 days'.
    async with TenantConn(tenant_id) as conn:
        row = await conn.fetchrow(
            "UPDATE projects SET status = 'pending_delete', deleted_at = now() "
            "WHERE id = $1 AND status != 'pending_delete' RETURNING id",
            project_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
        await audit.record(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            actor=actor,
            action="delete",
            entity_type="project",
            entity_id=project_id,
        )
    return {"id": str(project_id), "status": "pending_delete"}
