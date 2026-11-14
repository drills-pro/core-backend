from fastapi import APIRouter, Depends, Query

from app.core.authz import Action, AuthzContext, require
from app.modules.user.catalog import service
from app.modules.user.catalog.schemas import CatalogModel

router = APIRouter(prefix="/v1/catalog", tags=["catalog"])


@router.get("/models", response_model=list[CatalogModel])
async def list_catalog_models(
    task: str | None = Query(default=None, description="a task type slug"),
    ctx: AuthzContext = Depends(require(Action.PROJECT_LIST)),
):
    """The enabled base models training can start from, with the licence recorded at admission."""
    return await service.list_models(ctx, task)
