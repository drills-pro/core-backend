"""Training-run lifecycle: settle a run whose job ended (or ran out of time), cancel one, and the sweep that applies
settle to runs nobody is watching (app/background.py).

settle is decide -> effects -> persist (decide.py, effects.py, persist.py): the decision is pure, the effects run with
no pooled connection, and the write is one short transaction in which only the compare-and-set winner records the
model and the usage.
"""
import logging
import uuid
from http import HTTPStatus

from app.core.metrics import usage as usage_metrics
from app.core.problem import Problem
from app.core.tenant_conn import TenantConn
from app.integrations import job_lifecycle
from app.integrations.data_plane.train_report import FailureCode
from app.modules.user.training import decide, effects, persist
from app.modules.user.training.constants import TRANSITIONS, ProblemCode, TrainingStatus

logger = logging.getLogger(__name__)

_BY_ID = "SELECT * FROM training_runs WHERE id = $1"


async def settle(row: dict, verdict: job_lifecycle.Verdict) -> None:
    to = decide.target(row, verdict)
    if to is None:
        return
    tenant_id = str(row["tenant_id"])
    report = await effects.final_report(tenant_id, row) if decide.needs_report(to, verdict.timed_out) else None
    outcome = decide.resolve(to, report, verdict.timed_out)
    await effects.close_mlflow(tenant_id, row, outcome)
    registered = None
    if outcome.status is TrainingStatus.SUCCEEDED:
        await effects.publish_model(tenant_id, row)
        registered = await effects.register_model(tenant_id, row)
    started_at = None
    if row.get("started_at") is None and verdict.ray in persist.RAN_TO_THE_END and not verdict.timed_out:
        started_at = await effects.ray_started_at(row)  # a job faster than the sweep was never seen running
    async with TenantConn(tenant_id) as conn:
        moved = await persist.apply(conn, row, outcome, registered, started_at)
    if moved is None:
        logger.info("run %s was moved by someone else; nothing to settle", row["id"])  # a cancel or another replica won
        return
    if not TRANSITIONS[outcome.status]:
        usage_metrics.training_runs.add(1, {"status": outcome.status})
    if verdict.timed_out:
        await job_lifecycle.stop_quietly(row["ray_job_id"])


async def cancel(tenant_id: str, row: dict) -> dict:
    """Close the run's MLflow run and move it to cancelled; the caller has stopped the job. When someone else moved
    it first (the sweep saw the stopped job), that is a success if it landed on cancelled, a 409 otherwise."""
    outcome = decide.Outcome(status=TrainingStatus.CANCELLED)
    await effects.close_mlflow(tenant_id, row, outcome)
    async with TenantConn(tenant_id) as conn:
        moved = await persist.apply(conn, row, outcome)
        if moved is None:
            current = await conn.fetchrow(_BY_ID, row["id"])
    if moved is None:
        if current is not None and current["status"] == TrainingStatus.CANCELLED:
            return dict(current)
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.INVALID_STATE, f"run is no longer {row['status']}")
    usage_metrics.training_runs.add(1, {"status": TrainingStatus.CANCELLED})
    return moved


async def fail_unstarted(tenant_id: str, run_id: uuid.UUID, mlflow_run_id: str, project_id: uuid.UUID) -> None:
    """The job never started: end the run as failed so it stops counting against the quota."""
    row = {"id": run_id, "status": TrainingStatus.SUBMITTED, "project_id": project_id, "mlflow_run_id": mlflow_run_id}
    report = decide.failed_report(FailureCode.INTERNAL, "the training job could not be started")
    outcome = decide.Outcome(status=TrainingStatus.FAILED, report=report)
    async with TenantConn(tenant_id) as conn:
        moved = await persist.apply(conn, row, outcome)
    if moved is not None:
        usage_metrics.training_runs.add(1, {"status": TrainingStatus.FAILED})
    await effects.close_mlflow(tenant_id, row, outcome)


async def reconcile_inflight_runs() -> None:
    """Follow every submitted/running run across all tenants, so a run nobody ever GETs individually still leaves
    'submitted' — otherwise it keeps counting against the tenant's MAX_CONCURRENT_TRAINING_RUNS quota."""
    await job_lifecycle.sweep(persist.TRAINING, settle)
