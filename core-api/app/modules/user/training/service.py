"""Training-run domain logic: the status transition every caller funnels
through, and the sweep that applies it to runs nobody is watching.

Kept out of routes.py because both an HTTP GET and the background poller
drive the same transition — see app/background.py.
"""
import logging
from datetime import datetime, timezone

from http import HTTPStatus

import httpx

from app.core.metrics import usage as usage_metrics
from app.core.problem import Problem
from app.integrations import compute, job_follower
from app.integrations.compute import JobStatus
from app.integrations.data_plane import mlflow
from app.modules.user.training import log_parsing
from app.modules.user.training.constants import INFLIGHT_STATUSES, TRANSITIONS, ProblemCode, TrainingStatus
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
_FINISHED = (TrainingStatus.SUCCEEDED, TrainingStatus.FAILED)


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
    if new_status not in TRANSITIONS[frm]:
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.INVALID_STATE, f"run is {frm}, it cannot become {new_status}")
    completed_at = datetime.now(timezone.utc) if not TRANSITIONS[new_status] else None  # any end state

    model_artifact = None  # only set below when freshly parsed; COALESCE in the UPDATE preserves any existing value
    metrics = None
    mlflow_run_id = row["mlflow_run_id"]
    if new_status == TrainingStatus.CANCELLED and mlflow_run_id:
        try:  # best effort: a tracking hiccup must not block a cancel
            await mlflow.finish_run(tenant_id, str(row["project_id"]), mlflow_run_id, mlflow.RunStatus.KILLED)
        except httpx.HTTPError:
            logger.warning("could not close MLflow run %s as killed", mlflow_run_id, exc_info=True)
    if new_status in _FINISHED:
        logs = await compute.logs(row["ray_job_id"])
        scalar_metrics = log_parsing.parse_metrics(logs)
        # epoch_history/confusion_matrix are structured, not floats — MLflow's
        # log-metric API only accepts scalars, so they're stored on our own
        # row (below) but never sent to MLflow.
        metrics = {**scalar_metrics, **log_parsing.parse_extra_metrics(logs)}
        model_artifact = log_parsing.parse_model_artifact(logs)
        if mlflow_run_id is not None:
            await mlflow.finish_run(
                tenant_id,
                str(row["project_id"]),
                mlflow_run_id,
                mlflow.RunStatus.FINISHED if new_status == TrainingStatus.SUCCEEDED else mlflow.RunStatus.FAILED,
                scalar_metrics,
            )
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
                source_ref=row["ray_job_id"],
            )

    if not TRANSITIONS[new_status]:
        usage_metrics.training_runs.add(1, {"status": new_status})

    updated = await conn.fetchrow(
        "UPDATE training_runs SET status = $2, mlflow_run_id = $3, "
        "completed_at = COALESCE(completed_at, $4), model_artifact = COALESCE(model_artifact, $5), "
        "metrics = COALESCE(metrics, $6) "
        "WHERE id = $1 AND status = $7 RETURNING *",
        row["id"],
        new_status,
        mlflow_run_id,
        completed_at,
        model_artifact,
        metrics,
        frm,
    )
    if updated is None:
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.INVALID_STATE, f"run is no longer {frm}")
    return dict(updated)


async def _select(conn, tenant_id):
    return await conn.fetch(_INFLIGHT, tenant_id, [str(s) for s in INFLIGHT_STATUSES])


async def _settle(conn, row: dict) -> None:
    ray_status = await job_follower.poll(row["ray_job_id"])
    if ray_status is None:
        return
    new_status = RAY_TO_STATUS[ray_status]
    if new_status in TRANSITIONS[TrainingStatus(row["status"])]:  # not e.g. running -> submitted
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
