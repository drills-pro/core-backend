"""Settles promotions to Production (planning/27), run by the poller in app/background.py.

`model_registry.promote` marks a version pending; this deploys it (service.ensure_deployed) and, only once that deployment is active,
flips the stages (model_registry.complete_promotion). The version that was serving keeps serving until then: a failure at any step
leaves every stage as it was, puts the project's `main` deployment back on the old version, and shows the error on the promoted version.
It lives here because deployments already depends on model_registry, not the other way round.
"""
import logging
import uuid

from app.core.authz import AuthzContext, TenantRole
from app.core.problem import Problem
from app.core.tenant_conn import TenantConn
from app.integrations import job_lifecycle
from app.modules.user.deployments import service
from app.modules.user.deployments.constants import PROMOTION_FAILED_DETAIL
from app.modules.user.model_registry import (
    PROMOTION_TENANTS_SQL,
    complete_promotion,
    fail_promotion,
    pending_promotions,
    serving_version,
)

logger = logging.getLogger(__name__)


def _reason(error: Exception) -> tuple[str, str | None]:
    """(what the customer is told, its problem code): a Problem's own detail (quota, risk, no servable model), never an internal error's words."""
    return (error.detail, str(error.code)) if isinstance(error, Problem) else (PROMOTION_FAILED_DETAIL, None)


async def _settle(tenant_id: str, row) -> None:
    project_id, version_id = row["project_id"], row["id"]
    ctx = AuthzContext(tenant_id, row["promotion_actor"] or "", TenantRole.MEMBER, project_id, frozenset())
    async with TenantConn(tenant_id) as conn:
        serving = await serving_version(conn, version_id)
    try:
        await service.ensure_deployed(ctx, project_id, version_id, row["promotion_acknowledged_risk"])
        await complete_promotion(ctx, project_id, version_id)
    except Exception as e:  # noqa: BLE001 - whatever failed, the promotion has failed: say so on the version
        logger.warning("promotion of version %s failed", version_id, exc_info=True)
        if serving is not None:
            await _put_back(ctx, project_id, serving)
        await fail_promotion(ctx, project_id, version_id, *_reason(e))


async def _put_back(ctx: AuthzContext, project_id: uuid.UUID, serving: uuid.UUID) -> None:
    """Deploy the version that was serving again: a no-op when it still has its own deployment, a move back for a `main` deployment
    the promotion had pointed at the new version."""
    try:
        await service.ensure_deployed(ctx, project_id, serving, acknowledge_risk=True)
    except Exception:  # noqa: BLE001
        logger.exception("could not put the deployment back on version %s", serving)


async def process_promotions() -> None:
    """Settle every pending promotion of the tenants that have one, oldest first. One failing never stops the rest."""
    for (tenant_id,) in await job_lifecycle.fetch(PROMOTION_TENANTS_SQL):
        async with TenantConn(str(tenant_id)) as conn:
            pending = await pending_promotions(conn, str(tenant_id))
        for row in pending:
            try:
                await _settle(str(tenant_id), row)
            except Exception:  # noqa: BLE001
                logger.exception("could not settle the promotion of version %s", row["id"])
