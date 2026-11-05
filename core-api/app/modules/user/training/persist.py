"""What a settling training run writes to Postgres, in the caller's short TenantConn. No gateway call and no
decisions: those are effects.py's and decide.py's."""
import logging
from datetime import UTC, datetime, timedelta

from app.integrations import job_lifecycle
from app.integrations.job_spec import Resources
from app.modules.user.model_registry import RegisteredVersion, record_version
from app.modules.user.training.constants import (
    INFLIGHT_STATUSES,
    TRANSITIONS,
    TrainingStatus,
)
from app.modules.user.training.decide import Outcome
from app.modules.user.training.train_spec import horizon_seconds
from app.modules.user.usage import UsageResource, UsageUnit, record_usage

logger = logging.getLogger(__name__)

# A run Ray no longer answers for (garbage-collected, head restarted, never submitted) must still be able to end.
TRAINING = job_lifecycle.JobTable(
    name="training",
    table="training_runs",
    tenants_fn="inflight_training_tenant_ids",
    inflight=INFLIGHT_STATUSES,
    transitions=TRANSITIONS,
    columns=frozenset({"metrics", "report", "started_at"}),
    deadline=lambda row: row["created_at"] + timedelta(seconds=horizon_seconds()),
)


def gpu_seconds(run: dict) -> float:
    """Billable GPU-seconds of an ended run (planning 14): seconds from the move to running to the end, times the
    GPU fraction its spec declared. 0 for a run never seen running (queue time is not billed) and for a CPU run."""
    started_at, completed_at = run.get("started_at"), run["completed_at"]
    if started_at is None or not run.get("spec"):
        return 0.0
    gpu = Resources.model_validate(run["spec"]["resources"]).gpu
    return max((completed_at - started_at).total_seconds(), 0.0) * gpu


async def apply(conn, row: dict, outcome: Outcome, registered: RegisteredVersion | None = None) -> dict | None:
    """Move the run to the outcome; None when someone else moved it first, in which case nothing else is written.
    Only the writer that wins the move records the model version (already created in MLflow) and bills, so a double
    settle does neither twice. Every end of a run that had started is billed, whatever its status."""
    cols = {}
    if outcome.report is not None:
        cols = {"metrics": outcome.report.metrics, "report": outcome.report.model_dump(mode="json")}
    if outcome.status is TrainingStatus.RUNNING:
        cols["started_at"] = datetime.now(UTC)
    moved = await job_lifecycle.advance(conn, TRAINING, row, outcome.status, **cols)
    if moved is None or TRANSITIONS[outcome.status]:
        return moved
    tenant_id = str(row["tenant_id"])
    if registered is not None and outcome.status is TrainingStatus.SUCCEEDED:
        await record_version(conn, tenant_id, row["project_id"], row["id"], registered)
    quantity = gpu_seconds(moved)
    if moved.get("started_at") is None:
        logger.info("run %s ended without being seen running: no compute billed", row["id"])
    if quantity > 0:
        await record_usage(
            conn,
            tenant_id=tenant_id,
            project_id=row["project_id"],
            resource_type=UsageResource.TRAINING,
            quantity=quantity,
            unit=UsageUnit.GPU_SECONDS,
            occurred_at=moved["completed_at"],
            source_ref=row["ray_job_id"],  # the Ray submission id: one per run, so a second settle bills nothing
        )
    return moved
