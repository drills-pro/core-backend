"""Training-run reads and cancel. Status transitions stay in service.py; cancel goes through transition()."""
import uuid

from fastapi import HTTPException

from app.core.authz import AuthzContext
from app.core.tenant_conn import TenantConn
from app.integrations import compute
from app.modules.user.training import service
from app.modules.user.training.constants import CANCELLABLE, CANCELLED
from app.modules.user.training.schemas import (
    LineageDeployment,
    LineageModel,
    LineageRun,
    RunLogRef,
    TrainingRun,
    TrainingRunLineage,
    TrainingRunSummary,
)

_NOT_FOUND = "training run not found"


async def list_runs(ctx: AuthzContext, project_id: uuid.UUID, limit: int, offset: int) -> list[TrainingRunSummary]:
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT id, status, dataset_ref, ray_job_id, mlflow_run_id, config, metrics, created_at, completed_at "
            "FROM training_runs WHERE project_id = $1 "
            "ORDER BY created_at DESC LIMIT $2 OFFSET $3",
            project_id,
            limit,
            offset,
        )
    return [TrainingRunSummary.model_validate(dict(r)) for r in rows]


async def get_run(ctx: AuthzContext, project_id: uuid.UUID, run_id: uuid.UUID) -> TrainingRun:
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow("SELECT * FROM training_runs WHERE id = $1 AND project_id = $2", run_id, project_id)
    if row is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return TrainingRun.model_validate(dict(row))


async def get_log_ref(ctx: AuthzContext, project_id: uuid.UUID, run_id: uuid.UUID) -> RunLogRef:
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT ray_job_id, status FROM training_runs WHERE id = $1 AND project_id = $2", run_id, project_id
        )
    if row is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return RunLogRef.model_validate(dict(row))


async def get_lineage(ctx: AuthzContext, project_id: uuid.UUID, run_id: uuid.UUID) -> TrainingRunLineage:
    async with TenantConn(ctx.tenant_id) as conn:
        run = await conn.fetchrow(
            "SELECT id, status, dataset_id, dataset_version, test_dataset_id, test_dataset_version, "
            "mlflow_run_id, reproduction_group_id, config, created_at FROM training_runs "
            "WHERE id = $1 AND project_id = $2",
            run_id,
            project_id,
        )
        if run is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)

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
    return TrainingRunLineage(
        training_run=LineageRun.model_validate(dict(run)),
        dataset_commit_message=None,  # ponytail: lineage joins dataset_versions once training is rewired
        model_version=LineageModel.model_validate(dict(model)) if model else None,
        deployment=LineageDeployment.model_validate(dict(deployment)) if deployment else None,
    )


async def cancel_run(ctx: AuthzContext, project_id: uuid.UUID, run_id: uuid.UUID) -> TrainingRun:
    """Stop the Ray job, then move the row to cancelled through the shared transition (MLflow close, metric)."""
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow("SELECT * FROM training_runs WHERE id = $1 AND project_id = $2", run_id, project_id)
        if row is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        if row["status"] not in CANCELLABLE:
            raise HTTPException(status_code=409, detail=f"run already {row['status']}")
        await compute.stop(row["ray_job_id"])
        updated = await service.transition(conn, dict(row), CANCELLED, ctx.tenant_id)
    return TrainingRun.model_validate(updated)
