"""What an admin may do to the plan limits: one transaction per write, with its admin_audit row."""
from app.core import admin_audit, plan_limits
from app.core.enums import AdminAuditAction, AdminAuditEntity, PlanTier
from app.core.plan_limits import PlanLimits, PlanLimitsBody


async def put(conn, tier: PlanTier, body: PlanLimitsBody, actor: str) -> PlanLimits:
    """Create or replace one tier's limits; they apply to the next request of every tenant on that tier."""
    async with conn.transaction():
        row = await plan_limits.put(conn, tier, body, actor)
        await admin_audit.record(conn, actor, AdminAuditAction.PLAN_LIMITS_PUT, AdminAuditEntity.PLAN_LIMITS, tier, body.model_dump(mode="json"))
    return row
