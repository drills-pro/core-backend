"""Background reconciliation for deployments — see app/background.py."""
import logging
from datetime import timedelta

from app.core.tenant_conn import TenantConn
from app.integrations import job_lifecycle
from app.modules.user.deployments.status import DeploymentStatus

logger = logging.getLogger(__name__)

S, Phase = DeploymentStatus, job_lifecycle.Phase
DEPLOY_DEADLINE_SECONDS = 1800  # from the deploy job's submission; a job Ray no longer answers for fails after this
DEPLOYMENTS = job_lifecycle.JobTable(
    name="deployment",
    table="deployments",
    tenants_fn="inflight_deploy_tenant_ids",
    inflight=(S.DEPLOYING,),
    transitions={S.DEPLOYING: frozenset({S.ACTIVE, S.FAILED})},
    completed_col=None,  # the table has no completed_at
    deadline=lambda row: row["deploying_since"] + timedelta(seconds=DEPLOY_DEADLINE_SECONDS),
)
# A job someone stopped never serves, so it fails like one that crashed.
_PHASE_TO_STATUS = {Phase.SUCCEEDED: S.ACTIVE, Phase.FAILED: S.FAILED, Phase.STOPPED: S.FAILED}


async def _settle(row: dict, verdict: job_lifecycle.Verdict) -> None:
    to = S.FAILED if verdict.timed_out else _PHASE_TO_STATUS.get(verdict.phase)
    if to is None:
        return
    async with TenantConn(str(row["tenant_id"])) as conn:
        moved = await job_lifecycle.advance(conn, DEPLOYMENTS, row, to)
    if moved is None:
        logger.info("deployment %s was moved by someone else; nothing to settle", row["id"])
    elif verdict.timed_out:
        await job_lifecycle.stop_quietly(row["ray_job_id"])


async def reconcile_inflight_deployments() -> None:
    """Follow every deploying deployment: active once Ray Serve finished starting it, failed when its job failed,
    was stopped, or outran DEPLOY_DEADLINE_SECONDS (a job Ray garbage-collected would otherwise stay 'deploying'
    forever). Nobody has to GET the deployment for this to happen."""
    await job_lifecycle.sweep(DEPLOYMENTS, _settle)
