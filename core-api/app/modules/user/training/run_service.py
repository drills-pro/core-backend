"""Training-run reads and cancel. Status transitions stay in service.py; cancel goes through service.cancel()."""
import uuid
from http import HTTPStatus

from app.core.authz import AuthzContext
from app.core.tenant_conn import TenantConn
from app.integrations import compute
from app.core.problem import Problem, not_found
from app.modules.user.model_registry import ModelVersion, register_run
from app.modules.user.training import events_service, service
from app.modules.user.training.constants import CANCELLABLE, LOG_TAIL_CHARS, NOT_REGISTRABLE_DETAIL, ProblemCode
from app.modules.user.training.readers import get_run_model
from app.modules.user.training.schemas import (
    RunLogRef,
    TrainingRun,
    TrainingRunLogs,
    TrainingRunSummary,
)



async def list_runs(ctx: AuthzContext, project_id: uuid.UUID, limit: int, offset: int) -> list[TrainingRunSummary]:
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT id, status, dataset_ref, dataset_version_id, preset, ray_job_id, mlflow_run_id, config, metrics, created_at, completed_at "
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
            raise not_found()
        progress = await events_service.progress_of(conn, run_id)
    return TrainingRun.model_validate({**dict(row), "progress": progress})


async def get_log_ref(ctx: AuthzContext, project_id: uuid.UUID, run_id: uuid.UUID) -> RunLogRef:
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT ray_job_id, status FROM training_runs WHERE id = $1 AND project_id = $2", run_id, project_id
        )
    if row is None:
        raise not_found()
    return RunLogRef.model_validate(dict(row))


async def get_logs(ctx: AuthzContext, project_id: uuid.UUID, run_id: uuid.UUID) -> TrainingRunLogs:
    """The end of the job's raw output; no connection is held while Ray answers."""
    ref = await get_log_ref(ctx, project_id, run_id)
    logs = await compute.logs(ref.ray_job_id)
    return TrainingRunLogs(status=ref.status, logs=logs[-LOG_TAIL_CHARS:])


async def cancel_run(ctx: AuthzContext, project_id: uuid.UUID, run_id: uuid.UUID) -> TrainingRun:
    """Stop the Ray job, then move the row to cancelled through service.cancel (MLflow close, metric); no connection
    is held across either."""
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow("SELECT * FROM training_runs WHERE id = $1 AND project_id = $2", run_id, project_id)
    if row is None:
        raise not_found()
    if row["status"] not in CANCELLABLE:
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.INVALID_STATE, f"run already {row['status']}")
    await compute.stop(row["ray_job_id"])
    return TrainingRun.model_validate(await service.cancel(ctx.tenant_id, dict(row)))


async def register_model(ctx: AuthzContext, project_id: uuid.UUID, run_id: uuid.UUID, name: str) -> ModelVersion:
    """Register a succeeded run's model in the registry (the registry never reads training itself)."""
    async with TenantConn(ctx.tenant_id) as conn:
        run = await get_run_model(conn, project_id, run_id)
    if run is None:
        raise not_found()
    if not run.has_model:
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.NOT_REGISTRABLE, NOT_REGISTRABLE_DETAIL)
    return await register_run(ctx, project_id, run_id, name, run.mlflow_run_id)
