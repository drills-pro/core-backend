"""Background reconciliation for dataset imports — see app/background.py."""
import logging

from app.core.db import get_pool
from app.core.tenant_conn import TenantConn
from app.integrations import ray_client

logger = logging.getLogger(__name__)


async def reconcile_inflight_imports() -> None:
    """Same pattern as app/modules/training/routes.py's reconcile_inflight_runs — nothing
    else polls Ray for an import's status, so without this a running
    import would sit at 'submitted'/'running' forever in the DB even
    after Ray finished it."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        tenant_ids = [r["inflight_import_tenant_ids"] for r in await conn.fetch("SELECT inflight_import_tenant_ids()")]

    ray_to_status = {
        "PENDING": "submitted",
        "RUNNING": "running",
        "SUCCEEDED": "succeeded",
        "FAILED": "failed",
        "STOPPED": "failed",
    }
    for tenant_id in tenant_ids:
        async with TenantConn(str(tenant_id)) as conn:
            rows = await conn.fetch(
                "SELECT * FROM dataset_imports WHERE tenant_id = $1 AND status IN ('submitted', 'running')",
                tenant_id,
            )
            for row in rows:
                try:
                    ray_status = await ray_client.get_status(row["ray_job_id"])
                except Exception:  # noqa: BLE001 - one bad job shouldn't block the sweep
                    logger.exception("failed to poll ray status for import %s", row["id"])
                    continue
                new_status = ray_to_status.get(ray_status, row["status"])
                if new_status == row["status"]:
                    continue
                error = None
                if new_status == "failed":
                    logs = await ray_client.get_logs(row["ray_job_id"])
                    error = logs[-2000:]
                await conn.execute(
                    "UPDATE dataset_imports SET status = $2, error = $3, "
                    "completed_at = CASE WHEN $2 IN ('succeeded', 'failed') THEN now() ELSE completed_at END "
                    "WHERE id = $1",
                    row["id"],
                    new_status,
                    error,
                )
