"""HTTP surface for training runs. Domain transitions live in
app/modules/training/service.py, which the background poller shares.
"""
import logging
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.authz import Action, AuthzContext, require
from app.core.problem import Problem
from app.core.tenant_conn import TenantConn
from app.integrations import ray_client
from app.integrations.data_plane import mlflow

logger = logging.getLogger(__name__)

NOT_IMPLEMENTED = "not_implemented"

router = APIRouter(prefix="/v1/projects/{project_id}/training-runs", tags=["training"])

async def _close_run(tenant_id: str, project_id: uuid.UUID, mlflow_run_id: str | None, status: mlflow.RunStatus) -> None:
    """Best effort: a tracking hiccup must not mask the error or block a cancel."""
    if mlflow_run_id is None:
        return
    try:
        await mlflow.finish_run(tenant_id, str(project_id), mlflow_run_id, status)
    except httpx.HTTPError:
        logger.warning("could not close MLflow run %s as %s", mlflow_run_id, status, exc_info=True)


@router.post("", status_code=202)
async def create_training_run(
    project_id: uuid.UUID,
    ctx: AuthzContext = Depends(require(Action.JOB_CREATE)),
):
    # ponytail: data access is stubbed until the trainer slice rewires training to dataset version ids
    # (docs/datasets-plan.md, Deferred); authz above still applies.
    raise Problem(501, NOT_IMPLEMENTED, "training is not available until the trainer slice")


@router.get("")
async def list_training_runs(
    project_id: uuid.UUID,
    ctx: AuthzContext = Depends(require(Action.JOB_READ)),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT id, status, dataset_ref, ray_job_id, mlflow_run_id, config, metrics, created_at, completed_at "
            "FROM training_runs WHERE project_id = $1 "
            "ORDER BY created_at DESC LIMIT $2 OFFSET $3",
            project_id,
            limit,
            offset,
        )
    return [dict(r) for r in rows]


@router.get("/{run_id}")
async def get_training_run(
    project_id: uuid.UUID, run_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.JOB_READ))
):
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM training_runs WHERE id = $1 AND project_id = $2", run_id, project_id
        )
        if row is None:
            raise HTTPException(status_code=404, detail="training run not found")
        # Read-only: status sync is the background reconciler's job (service.py).
    return dict(row)


@router.get("/{run_id}/logs")
async def get_training_run_logs(
    project_id: uuid.UUID, run_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.JOB_READ))
):
    """Live progress while a run is still submitted/running — not just the
    final result. Ray's job-logs endpoint returns whatever a still-running
    job has printed so far, and parse_metrics/parse_extra_metrics are plain
    line scanners that already work fine on a partial log (Ultralytics'
    own per-epoch console output shows up here as the job progresses, no
    change needed in the training templates)."""
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT ray_job_id, status FROM training_runs WHERE id = $1 AND project_id = $2", run_id, project_id
        )
    if row is None:
        raise HTTPException(status_code=404, detail="training run not found")

    logs = await ray_client.get_logs(row["ray_job_id"])
    return {
        "status": row["status"],
        "logs": logs[-8000:],
        "metrics": ray_client.parse_metrics(logs),
        "extra_metrics": ray_client.parse_extra_metrics(logs),
    }


@router.get("/{run_id}/lineage")
async def get_training_run_lineage(
    project_id: uuid.UUID, run_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.JOB_READ))
):
    """FR1 (05-reproducibility-lineage.md): one endpoint joining dataset
    commit -> training run -> model registry entry -> active deployment,
    instead of four separate lookups a user has to correlate by hand."""
    async with TenantConn(ctx.tenant_id) as conn:
        run = await conn.fetchrow(
            "SELECT id, status, dataset_id, dataset_version, test_dataset_id, test_dataset_version, "
            "mlflow_run_id, reproduction_group_id, config, created_at FROM training_runs "
            "WHERE id = $1 AND project_id = $2",
            run_id,
            project_id,
        )
        if run is None:
            raise HTTPException(status_code=404, detail="training run not found")

        model = await conn.fetchrow(
            "SELECT id, mlflow_model_name, mlflow_version, stage FROM model_versions "
            "WHERE training_run_id = $1 ORDER BY created_at DESC LIMIT 1",
            run_id,
        )
        deployment = None
        if model is not None:
            deployment = await conn.fetchrow(
                "SELECT id, status, route_prefix FROM deployments WHERE training_run_id = $1 "
                "ORDER BY created_at DESC LIMIT 1",
                run_id,
            )

    return {
        "training_run": dict(run),
        "dataset_commit_message": None,  # ponytail: lineage joins dataset_versions once training is rewired
        "model_version": dict(model) if model else None,
        "deployment": dict(deployment) if deployment else None,
    }


@router.post("/{run_id}/cancel")
async def cancel_training_run(
    project_id: uuid.UUID, run_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.JOB_CANCEL))
):
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM training_runs WHERE id = $1 AND project_id = $2", run_id, project_id
        )
        if row is None:
            raise HTTPException(status_code=404, detail="training run not found")
        if row["status"] not in ("submitted", "running"):
            raise HTTPException(status_code=409, detail=f"run already {row['status']}")
        await ray_client.stop_job(row["ray_job_id"])
        updated = await conn.fetchrow(
            "UPDATE training_runs SET status = 'cancelled', completed_at = now() "
            "WHERE id = $1 RETURNING *",
            run_id,
        )
    await _close_run(ctx.tenant_id, project_id, row["mlflow_run_id"], mlflow.RunStatus.KILLED)
    return dict(updated)
