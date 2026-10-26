"""Background reconciliation for deployments — see app/background.py."""
from app.integrations import job_follower
from app.modules.user.deployments.status import DeploymentStatus

_INFLIGHT_TENANTS = "SELECT inflight_deploy_tenant_ids()"
_INFLIGHT = "SELECT id, ray_job_id FROM deployments WHERE tenant_id = $1 AND status = $2"
_SET_STATUS = "UPDATE deployments SET status = $2 WHERE id = $1"
_PHASE_TO_STATUS = {job_follower.Phase.SUCCEEDED: DeploymentStatus.ACTIVE, job_follower.Phase.FAILED: DeploymentStatus.FAILED}


async def _select(conn, tenant_id):
    return await conn.fetch(_INFLIGHT, tenant_id, DeploymentStatus.DEPLOYING)


async def _settle(conn, row: dict) -> None:
    ray_status = await job_follower.poll(row["ray_job_id"])
    new_status = _PHASE_TO_STATUS.get(job_follower.phase(ray_status)) if ray_status else None
    if new_status is not None:
        await conn.execute(_SET_STATUS, row["id"], new_status)


async def reconcile_inflight_deployments() -> None:
    """Same bug class as training runs / dataset imports had: nothing
    polled Ray for a deploy job's status unless the frontend GET'd that
    exact deployment, so a deployment nobody individually re-fetched sat
    at 'deploying' forever even after Ray Serve finished starting it —
    not a GPU-contention issue, just a missing poller."""
    await job_follower.follow(_INFLIGHT_TENANTS, _select, _settle)
