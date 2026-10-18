"""Background reconciliation for deployments — see app/background.py."""
from app.core.db import get_pool
from app.core.tenant_conn import TenantConn
from app.integrations import ray_client
from app.modules.user.deployments.status import DeploymentStatus


async def reconcile_inflight_deployments() -> None:
    """Same bug class as training runs / dataset imports had: nothing
    polled Ray for a deploy job's status unless the frontend GET'd that
    exact deployment, so a deployment nobody individually re-fetched sat
    at 'deploying' forever even after Ray Serve finished starting it —
    not a GPU-contention issue, just a missing poller."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        tenant_ids = [r["inflight_deploy_tenant_ids"] for r in await conn.fetch("SELECT inflight_deploy_tenant_ids()")]

    for tenant_id in tenant_ids:
        async with TenantConn(str(tenant_id)) as conn:
            rows = await conn.fetch(
                "SELECT id, ray_job_id FROM deployments WHERE tenant_id = $1 AND status = $2", tenant_id, DeploymentStatus.DEPLOYING
            )
            for row in rows:
                try:
                    ray_status = await ray_client.get_status(row["ray_job_id"])
                except Exception:  # noqa: BLE001 - one bad job shouldn't block the sweep
                    continue
                if ray_status == "SUCCEEDED":
                    new_status = DeploymentStatus.ACTIVE
                elif ray_status == "FAILED":
                    new_status = DeploymentStatus.FAILED
                else:
                    continue
                await conn.execute("UPDATE deployments SET status = $2 WHERE id = $1", row["id"], new_status)
