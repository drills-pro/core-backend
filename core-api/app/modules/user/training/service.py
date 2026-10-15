"""Training-run domain logic: the status transition every caller funnels
through, and the sweep that applies it to runs nobody is watching.

Kept out of routes.py because both an HTTP GET and the background poller
drive the same transition — see app/background.py.
"""
import logging
from datetime import datetime, timezone

from app.core import metrics as app_metrics
from app.core.db import get_pool
from app.core.tenant_conn import TenantConn
from app.integrations import mlflow_client, ray_client
from app.modules.user.usage import record_usage

logger = logging.getLogger(__name__)

RAY_TO_STATUS = {
    "PENDING": "submitted",
    "RUNNING": "running",
    "SUCCEEDED": "succeeded",
    "FAILED": "failed",
    "STOPPED": "cancelled",
}


async def transition(conn, row: dict, new_status: str, tenant_id: str) -> dict:
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
    completed_at = datetime.now(timezone.utc) if new_status in ("succeeded", "failed") else None

    model_artifact = None  # only set below when freshly parsed; COALESCE in the UPDATE preserves any existing value
    metrics = None
    if new_status in ("succeeded", "failed") and row["mlflow_run_id"] is None:
        logs = await ray_client.get_logs(row["ray_job_id"])
        scalar_metrics = ray_client.parse_metrics(logs)
        # epoch_history/confusion_matrix are structured, not floats — MLflow's
        # log-metric API only accepts scalars, so they're stored on our own
        # row (below) but never sent to MLflow.
        metrics = {**scalar_metrics, **ray_client.parse_extra_metrics(logs)}
        model_artifact = ray_client.parse_model_artifact(logs)
        mlflow_run_id = await mlflow_client.log_training_run(
            tenant_id=tenant_id,
            project_id=str(row["project_id"]),
            dataset_ref=row["dataset_ref"],
            metrics=scalar_metrics,
            succeeded=(new_status == "succeeded"),
        )
        if new_status == "succeeded":
            elapsed = (completed_at - row["created_at"]).total_seconds()
            await usage.record_usage(
                conn,
                tenant_id=tenant_id,
                project_id=row["project_id"],
                resource_type="training",
                quantity=elapsed,
                unit="gpu_seconds",
                occurred_at=completed_at,
                source_ref=row["ray_job_id"],
            )
    else:
        mlflow_run_id = row["mlflow_run_id"]

    if new_status in ("succeeded", "failed", "cancelled"):
        app_metrics.training_runs_total.add(1, {"status": new_status})

    updated = await conn.fetchrow(
        "UPDATE training_runs SET status = $2, mlflow_run_id = $3, "
        "completed_at = COALESCE(completed_at, $4), model_artifact = COALESCE(model_artifact, $5), "
        "metrics = COALESCE(metrics, $6) "
        "WHERE id = $1 RETURNING *",
        row["id"],
        new_status,
        mlflow_run_id,
        completed_at,
        model_artifact,
        metrics,
    )
    return dict(updated)


async def reconcile_inflight_runs() -> None:
    """Poll Ray for every submitted/running run across all tenants, so a run
    nobody ever GETs individually still transitions out of 'submitted' —
    otherwise it sits there forever and keeps counting against the tenant's
    MAX_CONCURRENT_TRAINING_RUNS quota (see create_training_run)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        tenant_ids = [r["inflight_training_tenant_ids"] for r in await conn.fetch("SELECT inflight_training_tenant_ids()")]

    for tenant_id in tenant_ids:
        async with TenantConn(str(tenant_id)) as conn:
            rows = await conn.fetch(
                "SELECT * FROM training_runs WHERE tenant_id = $1 AND status IN ('submitted', 'running')",
                tenant_id,
            )
            for row in rows:
                row = dict(row)
                try:
                    ray_status = await ray_client.get_status(row["ray_job_id"])
                except Exception:  # noqa: BLE001 - one bad job shouldn't block the sweep
                    logger.exception("failed to poll ray status for run %s", row["id"])
                    continue
                new_status = RAY_TO_STATUS.get(ray_status, row["status"])
                if new_status != row["status"]:
                    await transition(conn, row, new_status, str(tenant_id))
