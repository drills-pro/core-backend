"""Starting a training run: validate the dataset version, stage the data and the spec, record the run, submit the job.

No pooled connection is held across a gateway call (the gateway checks ownership on a connection of its own).
The run id is chosen up front and the Ray submission id derives from it, so a job whose submit answer was lost
can still be stopped; the row is written before the submit, so the gateway can check it owns the run.
"""
import logging
import uuid
from http import HTTPStatus


from app.core import plan_limits, presets, task_types
from app.core.authz import AuthzContext
from app.core.enums import JobPackageName, TaskType
from app.core.idempotency import insert_once, replay
from app.core.problem import Problem, not_found
from app.core.task_types import TaskInfo
from app.core.tenant_conn import TenantConn
from app.integrations import compute, data_plane
from app.integrations.data_plane import mlflow
from app.modules.user.datasets import get_version_source
from app.modules.user.projects import get_task_type
from app.modules.user.training import events_service, service, train_spec
from app.modules.user.training.constants import (
    INFLIGHT_STATUSES,
    START_FAILED_DETAIL,
    TRAINING_LOCK_PREFIX,
    ProblemCode,
)
from app.modules.user.training.schemas import TrainingRun, TrainingRunCreate

logger = logging.getLogger(__name__)

_BY_KEY = "SELECT * FROM training_runs WHERE project_id = $1 AND idempotency_key = $2"
_LOCK_TENANT = "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))"
_INFLIGHT_COUNT = "SELECT count(*) FROM training_runs WHERE tenant_id = $1 AND status = ANY($2)"
_INSERT = (
    "INSERT INTO training_runs (id, tenant_id, project_id, idempotency_key, ray_job_id, mlflow_run_id, dataset_ref, "
    "config, dataset_id, dataset_version, dataset_version_id, task, preset, spec, package_sha256, events_token_hash) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16) "
    "ON CONFLICT (project_id, idempotency_key) DO NOTHING RETURNING *"
)


async def _check_quota(conn, tenant_id: str) -> None:
    if await conn.fetchval(_INFLIGHT_COUNT, tenant_id, [str(s) for s in INFLIGHT_STATUSES]) >= await plan_limits.max_training_runs(conn, tenant_id):
        raise Problem(HTTPStatus.TOO_MANY_REQUESTS, ProblemCode.TOO_MANY_RUNS, "too many training runs in progress, wait for one to finish")


async def _task_of(conn, project_task: str | None, body: TrainingRunCreate) -> TaskInfo:
    if project_task is None:
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.TASK_TYPE_MISSING, "this project has no task type, so it cannot train")
    task = TaskType(project_task)
    info = await task_types.require_supported(conn, task)
    if not info.trainable:
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.TASK_NOT_TRAINABLE, f"task type '{task}' cannot be trained yet")
    if body.task_type is not None and body.task_type != task:
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.TASK_TYPE_MISMATCH, f"this project's task type is '{task}'")
    return info


def _same_request(row, body: TrainingRunCreate) -> bool:
    return (
        row["dataset_version_id"] == body.dataset_version_id
        and row["preset"] == body.preset
        and all(row["config"][k] == v for k, v in body.hyperparameters.model_dump(exclude_none=True).items())  # the rest was the preset's default then
        and row["spec"]["payload"]["seed"] == body.seed
    )


def _replay(row, body: TrainingRunCreate) -> TrainingRun:
    return TrainingRun.model_validate(dict(replay(row, lambda r: _same_request(r, body), ProblemCode.IDEMPOTENCY_MISMATCH)))


async def _fail_unstarted(ctx: AuthzContext, run_id: uuid.UUID, mlflow_run_id: str, project_id: uuid.UUID) -> None:
    """The job never started: end the run as failed so it stops counting against the quota. If this fails too,
    the reconciler fails the run when Ray still has no job for it at its deadline."""
    try:
        await service.fail_unstarted(ctx.tenant_id, run_id, mlflow_run_id, project_id)
    except Exception:  # noqa: BLE001 - best effort, see above
        logger.exception("could not close training run %s after a failed start", run_id)


async def create_run(ctx: AuthzContext, project_id: uuid.UUID, body: TrainingRunCreate, idempotency_key: str) -> TrainingRun:
    tenant_id = ctx.tenant_id
    async with TenantConn(tenant_id) as conn:
        existing = await conn.fetchrow(_BY_KEY, project_id, idempotency_key)
        if existing is not None:
            return _replay(existing, body)
        info = await _task_of(conn, await get_task_type(conn, project_id), body)
        task = info.slug
        version = await get_version_source(conn, project_id, body.dataset_version_id)
        if version is None:  # another project's or tenant's version is indistinguishable from a missing one
            raise not_found()
        preset = await presets.get(conn, task, body.preset)
        if preset is None:
            raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.UNKNOWN_PRESET, f"task type '{task}' has no preset '{body.preset}'")
        hyperparameters = train_spec.resolve_hyperparameters(preset, body.hyperparameters)
        await _check_quota(conn, tenant_id)  # cheap early answer; the one that counts is under the lock below

    run_id = uuid.uuid4()
    token = events_service.mint(run_id)  # the secret goes to the job's environment, its hash to the row, neither to the spec
    try:
        package = await data_plane.resolve_stable_package(JobPackageName.RAY_TRAINING)  # pinned: the run keeps it whatever is promoted later
        started = await mlflow.start_run(tenant_id, str(project_id), str(version.dataset_id), str(body.dataset_version_id))
        spec = await train_spec.build(tenant_id, project_id, run_id, version.dataset_id, info, body, preset, hyperparameters, token and token.reporting)
    except Exception as e:
        raise _start_failed(e, run_id) from e

    async with TenantConn(tenant_id) as conn:
        await conn.fetchval(_LOCK_TENANT, f"{TRAINING_LOCK_PREFIX}{tenant_id}")  # a concurrent create waits here, then sees this one
        await _check_quota(conn, tenant_id)
        row, created = await insert_once(
            conn, _INSERT,
            (run_id, tenant_id, project_id, idempotency_key, spec.job_id, started.run_id, str(body.dataset_version_id),
             hyperparameters.model_dump(mode="json"), version.dataset_id, version.lakefs_commit, body.dataset_version_id,
             task, preset.name, spec.model_dump(mode="json"), package.sha256, token and token.hash),
            _BY_KEY, (project_id, idempotency_key),
        )
        if not created:  # lost a race with the same key: that one is the run
            return _replay(row, body)

    try:
        await data_plane.submit_train(tenant_id, str(project_id), str(run_id), spec, package.sha256, token and token.secret)
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
    if isinstance(e, Problem):
        return e
    logger.error("could not start training run %s", run_id, exc_info=e)
    return Problem(HTTPStatus.SERVICE_UNAVAILABLE, ProblemCode.START_FAILED, START_FAILED_DETAIL)
