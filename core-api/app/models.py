import uuid

from fastapi import APIRouter, Body, Depends, HTTPException

from app import mlflow_client
from app.tenancy import get_tenant_id
from app.tenant_conn import TenantConn

router = APIRouter(prefix="/v1/projects/{project_id}/models", tags=["models"])

# req/06 FR2: registry stages map to customer-facing states.
CUSTOMER_STAGE_LABELS = {"None": "internal only", "Staging": "candidate", "Production": "live/deployed"}
VALID_STAGES = set(CUSTOMER_STAGE_LABELS)


@router.post("/{training_run_id}/register", status_code=201)
async def register_model(
    project_id: uuid.UUID, training_run_id: uuid.UUID, name: str, tenant_id: str = Depends(get_tenant_id)
):
    async with TenantConn(tenant_id) as conn:
        run = await conn.fetchrow(
            "SELECT mlflow_run_id, status FROM training_runs WHERE id = $1 AND project_id = $2",
            training_run_id,
            project_id,
        )
        if run is None:
            raise HTTPException(status_code=404, detail="training run not found")
        if run["status"] != "succeeded" or run["mlflow_run_id"] is None:
            raise HTTPException(status_code=409, detail="training run has no completed MLflow run to register")

        mlflow_version = await mlflow_client.register_model_version(name=name, run_id=run["mlflow_run_id"])
        row = await conn.fetchrow(
            "INSERT INTO model_versions (tenant_id, project_id, training_run_id, mlflow_model_name, mlflow_version) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING id, mlflow_model_name, mlflow_version, stage, created_at",
            tenant_id,
            project_id,
            training_run_id,
            name,
            mlflow_version,
        )
    return dict(row)


@router.get("")
async def list_models(project_id: uuid.UUID, tenant_id: str = Depends(get_tenant_id)):
    async with TenantConn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT id, mlflow_model_name, mlflow_version, stage, created_at FROM model_versions "
            "WHERE project_id = $1 ORDER BY created_at DESC",
            project_id,
        )
    return [{**dict(r), "customer_label": CUSTOMER_STAGE_LABELS.get(r["stage"], r["stage"])} for r in rows]


@router.post("/{model_version_id}/promote")
async def promote_model(
    project_id: uuid.UUID, model_version_id: uuid.UUID, stage: str, tenant_id: str = Depends(get_tenant_id)
):
    """FR2: None (internal) -> Staging (candidate) -> Production (live).
    Also covers rollback — promoting an older version to Production is
    the rollback path (MLflow doesn't distinguish "promote" from
    "rollback" mechanically, and neither does this endpoint)."""
    if stage not in VALID_STAGES:
        raise HTTPException(status_code=400, detail=f"stage must be one of {sorted(VALID_STAGES)}")
    async with TenantConn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT mlflow_model_name, mlflow_version FROM model_versions WHERE id = $1 AND project_id = $2",
            model_version_id,
            project_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="model version not found")

        await mlflow_client.transition_stage(
            name=row["mlflow_model_name"], version=row["mlflow_version"], stage=stage
        )
        updated = await conn.fetchrow(
            "UPDATE model_versions SET stage = $2 WHERE id = $1 "
            "RETURNING id, mlflow_model_name, mlflow_version, stage",
            model_version_id,
            stage,
        )
    return {**dict(updated), "customer_label": CUSTOMER_STAGE_LABELS[stage]}


@router.patch("/{model_version_id}/publish")
async def publish_model(
    project_id: uuid.UUID,
    model_version_id: uuid.UUID,
    publish_metadata: bool | None = Body(default=None),
    publish_artifact: bool | None = Body(default=None),
    tenant_id: str = Depends(get_tenant_id),
):
    # FR7 (08-collaboration-visibility.md): metrics/config vs. the weights
    # artifact are separate disclosures, independently toggleable.
    if publish_metadata is None and publish_artifact is None:
        raise HTTPException(status_code=400, detail="nothing to update")
    async with TenantConn(tenant_id) as conn:
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
            raise HTTPException(status_code=404, detail="model version not found")
    return dict(row)
