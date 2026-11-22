import uuid

from fastapi import APIRouter, Depends

from app.core.authz import Action, AuthzContext, require
from app.core.pagination import Page, PageParams
from app.modules.user.teams import service
from app.modules.user.teams.schemas import Team

router = APIRouter(prefix="/v1/teams", tags=["teams"])


@router.get("", response_model=Page[Team])
async def list_teams(ctx: AuthzContext = Depends(require(Action.PROJECT_LIST)), page: PageParams = Depends()):
    return await service.list_teams(ctx, page)


@router.post("", status_code=201, response_model=Team)
async def create_team(name: str, ctx: AuthzContext = Depends(require(Action.TENANT_ADMIN))):
    return await service.create_team(ctx, name)


@router.get("/{team_id}", response_model=Team)
async def get_team(team_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.PROJECT_LIST))):
    return await service.get_team(ctx, team_id)
