"""Platform-wide plan limits. Rows are shared by every tenant, so these go through get_platform_admin and an unscoped
connection; the rest of the platform reads them through app/core/plan_limits.py."""
import logging

from fastapi import APIRouter, Depends

from app.core import plan_limits
from app.core.auth import get_platform_admin
from app.core.db import get_pool
from app.core.enums import PlanTier
from app.core.plan_limits import PlanLimits, PlanLimitsBody
from app.modules.admin.plan_limits import service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/plan-limits", tags=["admin"])


@router.get("", response_model=list[PlanLimits])
async def list_plan_limits(actor: str = Depends(get_platform_admin)):
    async with (await get_pool()).acquire() as conn:
        return await plan_limits.list_all(conn)


@router.put("/{tier}", response_model=PlanLimits)
async def put_plan_limits(tier: PlanTier, body: PlanLimitsBody, actor: str = Depends(get_platform_admin)):
    """Create or replace one tier's limits; they apply to the next request of every tenant on that tier."""
    async with (await get_pool()).acquire() as conn:
        row = await service.put(conn, tier, body, actor)
    logger.info("plan limits %s set by %s", tier, actor)
    return row
