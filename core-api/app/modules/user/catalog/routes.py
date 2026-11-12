from fastapi import APIRouter, Depends

from app.core.authz import Action, AuthzContext, require
from app.modules.user.catalog import service
from app.modules.user.catalog.schemas import CatalogModel

router = APIRouter(prefix="/v1/catalog", tags=["catalog"])


@router.get("/models", response_model=list[CatalogModel])
async def list_catalog_models(ctx: AuthzContext = Depends(require(Action.PROJECT_LIST))):
    """The base models training can start from, with the licence recorded at admission."""
    return await service.list_models(ctx)
