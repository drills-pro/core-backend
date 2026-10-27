"""Starting a training run: validate the dataset version, stage the data and the spec, record the run, submit the job.

No pooled connection is held across a gateway call (the gateway checks ownership on a connection of its own).
The run id is chosen up front and the Ray submission id derives from it, so a job whose submit answer was lost
can still be stopped; the row is written before the submit, so the gateway can check it owns the run.
"""
import logging
import uuid
from datetime import UTC, datetime
from http import HTTPStatus

from fastapi import HTTPException

from app.core.authz import AuthzContext
from app.core.enums import TRAINABLE_TASK_TYPES, TaskType, require_supported_task_type
from app.core.metrics import usage as usage_metrics
from app.core.problem import Problem, not_found
from app.core.settings import settings
from app.core.tenant_conn import TenantConn
from app.integrations import compute, data_plane
from app.integrations.data_plane import mlflow
from app.integrations.data_plane.train_report import FailureCode, TrainOutcome, TrainReport
from app.modules.user.training import train_spec
from app.modules.user.training.constants import (
    INFLIGHT_STATUSES,
    START_FAILED_DETAIL,
    TRAINING_LOCK_PREFIX,
    ProblemCode,
    TrainingStatus,
)
from app.modules.user.training.schemas import TrainingRun, TrainingRunCreate

logger = logging.getLogger(__name__)

_PROJECT_TASK = "SELECT task_type FROM projects WHERE id = $1"
_VERSION = (
    "SELECT v.dataset_id, v.lakefs_commit FROM dataset_versions v JOIN datasets d ON d.id = v.dataset_id "
    "WHERE v.id = $1 AND d.project_id = $2 AND d.deleted_at IS NULL"
)
_BY_KEY = "SELECT * FROM training_runs WHERE project_id = $1 AND idempotency_key = $2"
_LOCK_TENANT = "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))"
_INFLIGHT_COUNT = "SELECT count(*) FROM training_runs WHERE tenant_id = $1 AND status = ANY($2)"
_INSERT = (
    "INSERT INTO training_runs (id, tenant_id, project_id, idempotency_key, ray_job_id, mlflow_run_id, dataset_ref, "
    "config, dataset_id, dataset_version, dataset_version_id, task, preset, spec) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14) "
    "ON CONFLICT (project_id, idempotency_key) DO NOTHING RETURNING *"
)
_FAIL_UNSTARTED = (
    "UPDATE training_runs SET status = $2, completed_at = $3, report = $4 WHERE id = $1 AND status = $5 RETURNING id"
)


async def _check_quota(conn, tenant_id: str) -> None:
    if await conn.fetchval(_INFLIGHT_COUNT, tenant_id, [str(s) for s in INFLIGHT_STATUSES]) >= settings.ray.max_concurrent_training_runs:
        raise Problem(HTTPStatus.TOO_MANY_REQUESTS, ProblemCode.TOO_MANY_RUNS, "too many training runs in progress, wait for one to finish")


def _task_of(project_task: str | None, body: TrainingRunCreate) -> TaskType:
    if project_task is None:
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.TASK_TYPE_MISSING, "this project has no task type, so it cannot train")
    task = TaskType(project_task)
    require_supported_task_type(task)
    if task not in TRAINABLE_TASK_TYPES:
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.TASK_NOT_TRAINABLE, f"task type '{task}' cannot be trained yet")
    if body.task_type is not None and body.task_type != task:
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.TASK_TYPE_MISMATCH, f"this project's task type is '{task}'")
    return task


def _same_request(row, body: TrainingRunCreate) -> bool:
    return (
        row["dataset_version_id"] == body.dataset_version_id
        and row["preset"] == body.preset
        and row["config"] == body.hyperparameters.model_dump(mode="json")
        and row["spec"]["payload"]["seed"] == body.seed
    )


def _replay(row, body: TrainingRunCreate) -> TrainingRun:
    if not _same_request(row, body):
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.IDEMPOTENCY_MISMATCH, "this Idempotency-Key was used with a different request")
    return TrainingRun.model_validate(dict(row))


async def _fail_unstarted(ctx: AuthzContext, run_id: uuid.UUID, mlflow_run_id: str, project_id: uuid.UUID) -> None:
    """The job never started: end the run as failed so it stops counting against the quota. If this fails too,
    the reconciler fails the run when Ray still has no job for it at its deadline."""
    report = TrainReport(status=TrainOutcome.FAILED, code=FailureCode.INTERNAL, message="the training job could not be started")
    try:
        async with TenantConn(ctx.tenant_id) as conn:
            await conn.fetchval(
                _FAIL_UNSTARTED, run_id, TrainingStatus.FAILED, datetime.now(UTC), report.model_dump(mode="json"), TrainingStatus.SUBMITTED
            )
        usage_metrics.training_runs.add(1, {"status": TrainingStatus.FAILED})
        await mlflow.finish_run(ctx.tenant_id, str(project_id), mlflow_run_id, mlflow.RunStatus.FAILED)
    except Exception:  # noqa: BLE001 - best effort, see above
        logger.exception("could not close training run %s after a failed start", run_id)


async def create_run(ctx: AuthzContext, project_id: uuid.UUID, body: TrainingRunCreate, idempotency_key: str) -> TrainingRun:
    tenant_id = ctx.tenant_id
    async with TenantConn(tenant_id) as conn:
        existing = await conn.fetchrow(_BY_KEY, project_id, idempotency_key)
        if existing is not None:
            return _replay(existing, body)
        task = _task_of(await conn.fetchval(_PROJECT_TASK, project_id), body)
        version = await conn.fetchrow(_VERSION, body.dataset_version_id, project_id)
        if version is None:  # another project's or tenant's version is indistinguishable from a missing one
            raise not_found()
        await _check_quota(conn, tenant_id)  # cheap early answer; the one that counts is under the lock below

    run_id = uuid.uuid4()
    try:
        started = await mlflow.start_run(tenant_id, str(project_id), str(version["dataset_id"]), str(body.dataset_version_id))
        spec = await train_spec.build(tenant_id, project_id, run_id, version["dataset_id"], task, body)
    except Exception as e:
        raise _start_failed(e, run_id) from e

    async with TenantConn(tenant_id) as conn:
        await conn.fetchval(_LOCK_TENANT, f"{TRAINING_LOCK_PREFIX}{tenant_id}")  # a concurrent create waits here, then sees this one
        await _check_quota(conn, tenant_id)
        row = await conn.fetchrow(
            _INSERT, run_id, tenant_id, project_id, idempotency_key, spec.job_id, started.run_id, str(body.dataset_version_id),
            body.hyperparameters.model_dump(mode="json"), version["dataset_id"], version["lakefs_commit"], body.dataset_version_id,
            task, body.preset, spec.model_dump(mode="json"),
        )
        if row is None:  # lost a race with the same key: that one is the run
            return _replay(await conn.fetchrow(_BY_KEY, project_id, idempotency_key), body)

    try:
        await data_plane.submit_train(tenant_id, str(project_id), str(run_id), spec)
    except Exception as e:
        try:
            await compute.stop(spec.job_id)  # Ray may have accepted it
        except Exception:  # noqa: BLE001 - it may not exist
            logger.warning("could not stop training job %s", spec.job_id)
        await _fail_unstarted(ctx, run_id, started.run_id, project_id)
        raise _start_failed(e, run_id) from e
    return TrainingRun.model_validate(dict(row))


def _start_failed(e: Exception, run_id: uuid.UUID) -> Exception:
    """A Problem stays as it is; anything else (storage, MLflow, Ray, the database) becomes a 503 with the cause in the log."""
    if isinstance(e, HTTPException):
        return e
    logger.error("could not start training run %s", run_id, exc_info=e)
    return Problem(HTTPStatus.SERVICE_UNAVAILABLE, ProblemCode.START_FAILED, START_FAILED_DETAIL)
