"""What a settling training run writes to Postgres, in the caller's short TenantConn. No gateway call and no
decisions: those are effects.py's and decide.py's."""
from datetime import timedelta

from app.integrations import job_lifecycle
from app.modules.user.model_registry import RegisteredVersion, record_version
from app.modules.user.training.constants import (
    INFLIGHT_STATUSES,
    TRANSITIONS,
    TrainingStatus,
)
from app.modules.user.training.decide import Outcome
from app.modules.user.training.train_spec import horizon_seconds
from app.modules.user.usage import UsageResource, UsageUnit, record_usage

# A run Ray no longer answers for (garbage-collected, head restarted, never submitted) must still be able to end.
TRAINING = job_lifecycle.JobTable(
    name="training",
    table="training_runs",
    tenants_fn="inflight_training_tenant_ids",
    inflight=INFLIGHT_STATUSES,
    transitions=TRANSITIONS,
    columns=frozenset({"metrics", "report"}),
    deadline=lambda row: row["created_at"] + timedelta(seconds=horizon_seconds()),
)


async def apply(conn, row: dict, outcome: Outcome, registered: RegisteredVersion | None = None) -> dict | None:
    """Move the run to the outcome; None when someone else moved it first, in which case nothing else is written.
    Only the writer that wins the move records the model version (already created in MLflow) and bills, so a double
    settle does neither twice."""
    cols = {}
    if outcome.report is not None:
        cols = {"metrics": outcome.report.metrics, "report": outcome.report.model_dump(mode="json")}
    moved = await job_lifecycle.advance(conn, TRAINING, row, outcome.status, **cols)
    if moved is None or outcome.status is not TrainingStatus.SUCCEEDED:
        return moved
    tenant_id = str(row["tenant_id"])
    if registered is not None:
        await record_version(conn, tenant_id, row["project_id"], row["id"], registered)
    completed_at = moved["completed_at"]
    await record_usage(
        conn,
        tenant_id=tenant_id,
        project_id=row["project_id"],
        resource_type=UsageResource.TRAINING,
        quantity=(completed_at - row["created_at"]).total_seconds(),
        unit=UsageUnit.GPU_SECONDS,
        occurred_at=completed_at,
        source_ref=row["ray_job_id"],  # the Ray submission id: one per run, so a second settle bills nothing
    )
    return moved
