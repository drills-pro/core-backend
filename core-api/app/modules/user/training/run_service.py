"""Training-run reads and cancel. Status transitions stay in service.py; cancel goes through service.cancel()."""
import logging
import uuid
from http import HTTPStatus

from app.core import audit
from app.core.authz import AuthzContext
from app.core.pagination import Page, PageParams, SortDir, build_page, keyset_clause, order_by
from app.core.tenant_conn import TenantConn
from app.integrations import compute
from app.core.problem import Problem, not_found
from app.modules.user.training import events_service, service
from app.modules.user.training.constants import (
    CANCEL_FAILED_DETAIL, CANCELLABLE, AuditAction, EntityType, ProblemCode)
from app.modules.user.training.steps import steps_of
from app.modules.user.training.schemas import (
    TrainingRun,
    TrainingRunSteps,
    TrainingRunSummary,
)

logger = logging.getLogger(__name__)


async def list_runs(ctx: AuthzContext, project_id: uuid.UUID, page: PageParams) -> Page[TrainingRunSummary]:
    """The project's runs, newest first."""
    async with TenantConn(ctx.tenant_id) as conn:
        cond, args = keyset_clause(page.page_token, SortDir.DESC, first_param=2)
        rows = await conn.fetch(
            "SELECT id, status, dataset_ref, dataset_version_id, preset, ray_job_id, mlflow_run_id, config, metrics, created_at, completed_at "
            f"FROM training_runs WHERE project_id = $1 {cond} "
            f"ORDER BY {order_by(SortDir.DESC)} LIMIT {page.page_size + 1}",
            project_id,
            *args,
        )
    return build_page(rows, page.page_size, TrainingRunSummary)


async def get_run(ctx: AuthzContext, project_id: uuid.UUID, run_id: uuid.UUID) -> TrainingRun:
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow("SELECT * FROM training_runs WHERE id = $1 AND project_id = $2", run_id, project_id)
        if row is None:
            raise not_found()
        progress = await events_service.progress_of(conn, run_id)
    return TrainingRun.model_validate({**dict(row), "progress": progress})


async def get_steps(ctx: AuthzContext, project_id: uuid.UUID, run_id: uuid.UUID) -> TrainingRunSteps:
    """The run's named steps and, for a failed run, a failure code. Built from the row and its reported epochs, never from the
    job's output (the raw log can carry presigned URLs and paths)."""
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT status, report ->> 'code' AS code FROM training_runs WHERE id = $1 AND project_id = $2", run_id, project_id
        )
        if row is None:
            raise not_found()
        progress = await events_service.progress_of(conn, run_id)
    return TrainingRunSteps(status=row["status"], steps=steps_of(row["status"], progress, row["code"]))


async def cancel_run(ctx: AuthzContext, project_id: uuid.UUID, run_id: uuid.UUID) -> TrainingRun:
    """Stop the Ray job, then move the row to cancelled through service.cancel (MLflow close, metric); no connection
    is held across either."""
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow("SELECT * FROM training_runs WHERE id = $1 AND project_id = $2", run_id, project_id)
    if row is None:
        raise not_found()
    if row["status"] not in CANCELLABLE:
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.INVALID_STATE, f"run already {row['status']}")
    try:
        await compute.stop(row["ray_job_id"])
    except Exception as e:  # noqa: BLE001 - compute is down: leave the run as it is so the caller can retry, not a 500
        logger.error("could not stop training job %s", row["ray_job_id"], exc_info=e)
        raise Problem(HTTPStatus.SERVICE_UNAVAILABLE, ProblemCode.CANCEL_FAILED, CANCEL_FAILED_DETAIL) from e
    cancelled = await service.cancel(ctx.tenant_id, dict(row))
    async with TenantConn(ctx.tenant_id) as conn:
        await audit.record_for(conn, ctx, project_id, AuditAction.CANCEL, EntityType.TRAINING_RUN, run_id)
    return TrainingRun.model_validate(cancelled)
