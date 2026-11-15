"""Platform-wide plan limits. Rows are shared by every tenant, so these go through get_platform_admin and an unscoped
connection; the rest of the platform reads them through app/core/plan_limits.py."""
import logging

from fastapi import APIRouter, Depends

from app.core import admin_audit, plan_limits
from app.core.auth import get_platform_admin
from app.core.db import get_pool
from app.core.enums import AdminAuditAction, AdminAuditEntity, PlanTier
from app.core.plan_limits import PlanLimits, PlanLimitsBody

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/plan-limits", tags=["admin"])


@router.get("", response_model=list[PlanLimits])
async def list_plan_limits(actor: str = Depends(get_platform_admin)):
    async with (await get_pool()).acquire() as conn:
        return await plan_limits.list_all(conn)


@router.put("/{tier}", response_model=PlanLimits)
async def put_plan_limits(tier: PlanTier, body: PlanLimitsBody, actor: str = Depends(get_platform_admin)):
    """Create or replace one tier's limits; they apply to the next request of every tenant on that tier."""
    async with (await get_pool()).acquire() as conn, conn.transaction():
        row = await plan_limits.put(conn, tier, body, actor)
        await admin_audit.record(conn, actor, AdminAuditAction.PLAN_LIMITS_PUT, AdminAuditEntity.PLAN_LIMITS, tier, body.model_dump(mode="json"))
    logger.info("plan limits %s set by %s", tier, actor)
    return row
