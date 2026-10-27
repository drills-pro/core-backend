"""Training-run domain logic: the status transition every caller funnels
through, and the sweep that applies it to runs nobody is watching.

Kept out of routes.py because both an HTTP GET and the background poller
drive the same transition — see app/background.py.

A run ends with the job's report.json (docs/trainer-plan.md T2): Ray's state says the job ended, the report says
how. Success needs both.
"""
import logging
from datetime import datetime, timezone

from http import HTTPStatus

import httpx
from pydantic import ValidationError

from app.core.metrics import usage as usage_metrics
from app.core.problem import Problem
from app.integrations import data_plane, job_follower
from app.integrations.compute import JobStatus
from app.integrations.data_plane import mlflow
from app.integrations.data_plane.train_report import FailureCode, TrainOutcome, TrainReport
from app.modules.user.model_registry import register_version
from app.modules.user.training.constants import (
    DEFAULT_MODEL_NAME,
    INFLIGHT_STATUSES,
    TRANSITIONS,
    ProblemCode,
    TrainingStatus,
)
from app.modules.user.training.train_spec import horizon_seconds
from app.modules.user.usage import UsageResource, UsageUnit, record_usage

logger = logging.getLogger(__name__)

RAY_TO_STATUS = {
    JobStatus.PENDING: TrainingStatus.SUBMITTED,
    JobStatus.RUNNING: TrainingStatus.RUNNING,
    JobStatus.SUCCEEDED: TrainingStatus.SUCCEEDED,
    JobStatus.FAILED: TrainingStatus.FAILED,
    JobStatus.STOPPED: TrainingStatus.CANCELLED,
}
_INFLIGHT_TENANTS = "SELECT inflight_training_tenant_ids()"
_INFLIGHT = "SELECT * FROM training_runs WHERE tenant_id = $1 AND status = ANY($2)"
_HAS_MODEL = "SELECT 1 FROM model_versions WHERE training_run_id = $1"
_FINISHED = (TrainingStatus.SUCCEEDED, TrainingStatus.FAILED)


def _failed(message: str) -> TrainReport:
    return TrainReport(status=TrainOutcome.FAILED, code=FailureCode.INTERNAL, message=message)


async def _final_report(tenant_id: str, row: dict) -> TrainReport:
    """The job's report; a job that ended without a usable one has failed, whatever Ray says. A store that does not
    answer raises, and the next sweep tries again."""
    try:
        report = await data_plane.read_train_report(tenant_id, str(row["project_id"]), str(row["id"]))
    except ValidationError:
        logger.warning("run %s wrote a report core-api cannot read", row["id"], exc_info=True)
        return _failed("the training job wrote a report that could not be read")
    return report or _failed("the training job ended without a report")


async def _register_model(conn, tenant_id: str, row: dict, mlflow_run_id: str) -> None:
    """Put the trained model in MLflow and register it as a version; once per run, however often a retry gets here."""
    if await conn.fetchval(_HAS_MODEL, row["id"]) is not None:
        return
    await mlflow.publish_run_model(tenant_id, str(row["project_id"]), str(row["id"]), mlflow_run_id)
    await register_version(conn, tenant_id, row["project_id"], row["id"], mlflow_run_id, DEFAULT_MODEL_NAME)


async def transition(conn, row: dict, new_status: TrainingStatus, tenant_id: str) -> dict:
    """Handle a training run reaching (or moving toward) a terminal state:
    FR1 (06-ml-training): log to MLflow. FR1 (09-usage-metering): bill it,
    idempotently — this can run more than once for the same job across
    polls, record_usage's UNIQUE(resource_type, source_ref) absorbs that."""
    # Serialize with any other caller (GET handler, poller on another replica): the second one
    # blocks here until the first commits, then finds the row already moved and stops, instead
    # of logging the run to MLflow twice.
    locked = dict(await conn.fetchrow("SELECT * FROM training_runs WHERE id = $1 FOR UPDATE", row["id"]))
    if locked["status"] != row["status"]:
        return locked
    frm = TrainingStatus(row["status"])
    report = None  # only a run that ended on its own has one
    if new_status in _FINISHED:
        report = await _final_report(tenant_id, row)
        if report.status is not TrainOutcome.SUCCEEDED:
            new_status = TrainingStatus.FAILED  # Ray's SUCCEEDED only says the driver exited
    if new_status not in TRANSITIONS[frm]:
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.INVALID_STATE, f"run is {frm}, it cannot become {new_status}")
    completed_at = datetime.now(timezone.utc) if not TRANSITIONS[new_status] else None  # any end state

    metrics = None
    mlflow_run_id = row["mlflow_run_id"]
    if new_status == TrainingStatus.CANCELLED and mlflow_run_id:
        try:  # best effort: a tracking hiccup must not block a cancel
            await mlflow.finish_run(tenant_id, str(row["project_id"]), mlflow_run_id, mlflow.RunStatus.KILLED)
        except httpx.HTTPError:
            logger.warning("could not close MLflow run %s as killed", mlflow_run_id, exc_info=True)
    if report is not None:
        metrics = report.metrics  # scalars only: MLflow's log-metric API takes nothing else; history and the confusion matrix stay in `report`
        if mlflow_run_id is not None:
            await mlflow.finish_run(
                tenant_id,
                str(row["project_id"]),
                mlflow_run_id,
                mlflow.RunStatus.FINISHED if new_status == TrainingStatus.SUCCEEDED else mlflow.RunStatus.FAILED,
                metrics,
            )
            if new_status == TrainingStatus.SUCCEEDED:
                await _register_model(conn, tenant_id, row, mlflow_run_id)
        if new_status == TrainingStatus.SUCCEEDED:
            elapsed = (completed_at - row["created_at"]).total_seconds()
            await record_usage(
                conn,
                tenant_id=tenant_id,
                project_id=row["project_id"],
                resource_type=UsageResource.TRAINING,
                quantity=elapsed,
                unit=UsageUnit.GPU_SECONDS,
                occurred_at=completed_at,
                source_ref=row["ray_job_id"],  # the Ray submission id: one per run, so a second settle bills nothing
            )

    if not TRANSITIONS[new_status]:
        usage_metrics.training_runs.add(1, {"status": new_status})

    updated = await conn.fetchrow(
        "UPDATE training_runs SET status = $2, mlflow_run_id = $3, "
        "completed_at = COALESCE(completed_at, $4), metrics = COALESCE(metrics, $5), report = COALESCE(report, $6) "
        "WHERE id = $1 AND status = $7 RETURNING *",
        row["id"],
        new_status,
        mlflow_run_id,
        completed_at,
        metrics,
        report.model_dump(mode="json") if report is not None else None,
        frm,
    )
    if updated is None:
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.INVALID_STATE, f"run is no longer {frm}")
    return dict(updated)


async def _select(conn, tenant_id):
    return await conn.fetch(_INFLIGHT, tenant_id, [str(s) for s in INFLIGHT_STATUSES])


def _past_deadline(row: dict) -> bool:
    """A run Ray no longer answers for (garbage-collected, head restarted, never submitted) must still be able to end."""
    return (datetime.now(timezone.utc) - row["created_at"]).total_seconds() > horizon_seconds()


async def _settle(conn, row: dict) -> None:
    ray_status = await job_follower.poll(row["ray_job_id"])
    if ray_status is None:
        if _past_deadline(row):
            await _end(conn, row, TrainingStatus.FAILED)
        return
    new_status = RAY_TO_STATUS[ray_status]
    if new_status in TRANSITIONS[TrainingStatus(row["status"])]:  # not e.g. running -> submitted
        await _end(conn, row, new_status)


async def _end(conn, row: dict, new_status: TrainingStatus) -> None:
    try:
        await transition(conn, row, new_status, str(row["tenant_id"]))
    except Problem as exc:
        if exc.code != ProblemCode.INVALID_STATE:
            raise
        logger.info("run %s was moved by someone else; nothing to settle", row["id"])  # a cancel or another replica won


async def reconcile_inflight_runs() -> None:
    """Poll Ray for every submitted/running run across all tenants, so a run
    nobody ever GETs individually still transitions out of 'submitted' —
    otherwise it sits there forever and keeps counting against the tenant's
    MAX_CONCURRENT_TRAINING_RUNS quota (see create_training_run)."""
    await job_follower.follow(_INFLIGHT_TENANTS, _select, _settle)
