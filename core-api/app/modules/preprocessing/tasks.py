"""Background reconciliation for preprocessing jobs — see app/background.py."""
from app.core.db import get_pool
from app.core.tenant_conn import TenantConn
from app.integrations import lakefs_client, ray_client


async def reconcile_inflight_preprocessing() -> None:
    """Same reconciler pattern as every other async job type in this
    codebase (00's standing rule) — nothing else polls Ray for this job's
    status."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        tenant_ids = [r["inflight_preprocessing_tenant_ids"] for r in await conn.fetch("SELECT inflight_preprocessing_tenant_ids()")]

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
                "SELECT * FROM dataset_preprocessing_jobs WHERE tenant_id = $1 AND status IN ('submitted', 'running')",
                tenant_id,
            )
            for row in rows:
                try:
                    ray_status = await ray_client.get_status(row["ray_job_id"])
                except Exception:  # noqa: BLE001 - one bad job shouldn't block the sweep
                    continue
                new_status = ray_to_status.get(ray_status, row["status"])
                if new_status == row["status"]:
                    continue
                error = None
                output_ref = None
                if new_status == "succeeded":
                    if lakefs_client.has_uncommitted_changes(str(row["dataset_id"])):
                        commit_info = lakefs_client.commit(
                            str(row["dataset_id"]), "preprocessing: resize/dedupe/class_balance"
                        )
                        output_ref = commit_info.get("id")
                elif new_status == "failed":
                    logs = await ray_client.get_logs(row["ray_job_id"])
                    error = logs[-2000:]
                await conn.execute(
                    "UPDATE dataset_preprocessing_jobs SET status = $2, error = $3, output_ref = $4, "
                    "completed_at = CASE WHEN $2 IN ('succeeded', 'failed') THEN now() ELSE completed_at END "
                    "WHERE id = $1",
                    row["id"],
                    new_status,
                    error,
                    output_ref,
                )
