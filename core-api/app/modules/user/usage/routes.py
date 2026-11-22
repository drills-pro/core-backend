"""HTTP read surface for usage metering. The writer other modules
call lives in app/modules/usage/service.py.
"""
import uuid

from fastapi import APIRouter, Depends

from app.core.authz import Action, AuthzContext, require
from app.core.pagination import Page, PageParams
from app.modules.user.usage import service
from app.modules.user.usage.schemas import UsageEvent, UsageTotal

router = APIRouter(prefix="/v1", tags=["usage"])


@router.get("/projects/{project_id}/usage", response_model=Page[UsageEvent])
async def project_usage(
    project_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.USAGE_READ)), page: PageParams = Depends()
):
    return await service.project_events(ctx, project_id, page)


@router.get("/usage", response_model=list[UsageTotal])
async def org_usage(ctx: AuthzContext = Depends(require(Action.BILLING_MANAGE))):
    return await service.org_totals(ctx)


@router.get("/teams/{team_id}/usage", response_model=list[UsageTotal])
async def team_usage(team_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.BILLING_MANAGE))):
    return await service.team_totals(ctx, team_id)
