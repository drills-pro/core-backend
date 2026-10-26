import re
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.core.authz import Action, AuthzContext, require
from app.core.problem import not_found
from app.core.tenant_conn import TenantConn
from app.integrations.data_plane import mlflow
from app.integrations.data_plane.mlflow import ModelStage as Stage

router = APIRouter(prefix="/v1/projects/{project_id}/models", tags=["models"])

# req/06 FR2: registry stages map to customer-facing states.
CUSTOMER_STAGE_LABELS = {Stage.NONE: "internal only", Stage.STAGING: "candidate", Stage.PRODUCTION: "live/deployed"}
MODEL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,60}$")


def _present(row, tenant_id: str, project_id: uuid.UUID) -> dict:
    """Customers see the name they chose, not the tenant/project-namespaced MLflow name."""
    out = dict(row)
    out["mlflow_model_name"] = mlflow.display_name(out["mlflow_model_name"], tenant_id, str(project_id))
    return out


@router.post("/{training_run_id}/register", status_code=201)
async def register_model(
    project_id: uuid.UUID,
    training_run_id: uuid.UUID,
    name: str = Query(pattern=MODEL_NAME.pattern),
    ctx: AuthzContext = Depends(require(Action.MODEL_WRITE)),
):
    tenant_id = ctx.tenant_id
    async with TenantConn(tenant_id) as conn:
        run = await conn.fetchrow(
            "SELECT mlflow_run_id, status FROM training_runs WHERE id = $1 AND project_id = $2",
            training_run_id,
            project_id,
        )
        if run is None:
            raise not_found()
        if run["status"] != "succeeded" or run["mlflow_run_id"] is None:
            raise HTTPException(status_code=409, detail="training run has no completed MLflow run to register")

        # Registered model names are global in MLflow, so the tenant and project are part of the name.
        mlflow_name = mlflow.registered_model_name(tenant_id, str(project_id), name)
        mlflow_version = await mlflow.register_model_version(tenant_id, str(project_id), name, run["mlflow_run_id"])
        row = await conn.fetchrow(
            "INSERT INTO model_versions (tenant_id, project_id, training_run_id, mlflow_model_name, mlflow_version) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING id, mlflow_model_name, mlflow_version, stage, created_at",
            tenant_id,
            project_id,
            training_run_id,
            mlflow_name,
            mlflow_version,
        )
    return _present(row, tenant_id, project_id)


@router.get("")
async def list_models(project_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.MODEL_READ))):
    tenant_id = ctx.tenant_id
    async with TenantConn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT id, mlflow_model_name, mlflow_version, stage, created_at FROM model_versions "
            "WHERE project_id = $1 ORDER BY created_at DESC",
            project_id,
        )
    return [
        {**_present(r, tenant_id, project_id), "customer_label": CUSTOMER_STAGE_LABELS.get(r["stage"], r["stage"])}
        for r in rows
    ]


@router.post("/{model_version_id}/promote")
async def promote_model(
    project_id: uuid.UUID,
    model_version_id: uuid.UUID,
    stage: str,
    ctx: AuthzContext = Depends(require(Action.MODEL_PROMOTE)),
):
    """FR2: None (internal) -> Staging (candidate) -> Production (live).
    Also covers rollback — promoting an older version to Production is
    the rollback path (MLflow doesn't distinguish "promote" from
    "rollback" mechanically, and neither does this endpoint)."""
    tenant_id = ctx.tenant_id
    if stage not in set(Stage):
        raise HTTPException(status_code=400, detail=f"stage must be one of {sorted(Stage)}")
    async with TenantConn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT mlflow_model_name, mlflow_version FROM model_versions WHERE id = $1 AND project_id = $2",
            model_version_id,
            project_id,
        )
        if row is None:
            raise not_found()

        await mlflow.transition_stage(
            tenant_id,
            str(project_id),
            mlflow.display_name(row["mlflow_model_name"], tenant_id, str(project_id)),
            row["mlflow_version"],
            Stage(stage),
        )
        updated = await conn.fetchrow(
            "UPDATE model_versions SET stage = $2 WHERE id = $1 "
            "RETURNING id, mlflow_model_name, mlflow_version, stage",
            model_version_id,
            stage,
        )
    return {**_present(updated, tenant_id, project_id), "customer_label": CUSTOMER_STAGE_LABELS[stage]}


@router.patch("/{model_version_id}/publish")
async def publish_model(
    project_id: uuid.UUID,
    model_version_id: uuid.UUID,
    publish_metadata: bool | None = Body(default=None),
    publish_artifact: bool | None = Body(default=None),
    ctx: AuthzContext = Depends(require(Action.PROJECT_WRITE)),
):
    # FR7 (08-collaboration-visibility.md): metrics/config vs. the weights
    # artifact are separate disclosures, independently toggleable.
    if publish_metadata is None and publish_artifact is None:
        raise HTTPException(status_code=400, detail="nothing to update")
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "UPDATE model_versions SET publish_metadata = COALESCE($2, publish_metadata), "
            "publish_artifact = COALESCE($3, publish_artifact) WHERE id = $1 AND project_id = $4 "
            "RETURNING id, publish_metadata, publish_artifact",
            model_version_id,
            publish_metadata,
            publish_artifact,
            project_id,
        )
        if row is None:
            raise not_found()
    return dict(row)
