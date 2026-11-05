"""HTTP read surface for usage metering. The writer other modules
call lives in app/modules/usage/service.py.
"""
import uuid

from fastapi import APIRouter, Depends, Query

from app.core.authz import Action, AuthzContext, require
from app.modules.user.usage import service
from app.modules.user.usage.schemas import UsageEvent, UsageTotal

router = APIRouter(prefix="/v1", tags=["usage"])


@router.get("/projects/{project_id}/usage", response_model=list[UsageEvent])
async def project_usage(
    project_id: uuid.UUID,
    ctx: AuthzContext = Depends(require(Action.USAGE_READ)),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    return await service.project_events(ctx, project_id, limit, offset)


@router.get("/usage", response_model=list[UsageTotal])
async def org_usage(ctx: AuthzContext = Depends(require(Action.BILLING_MANAGE))):
    return await service.org_totals(ctx)


@router.get("/teams/{team_id}/usage", response_model=list[UsageTotal])
async def team_usage(team_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.BILLING_MANAGE))):
    return await service.team_totals(ctx, team_id)
