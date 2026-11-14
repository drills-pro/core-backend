"""Platform-wide model catalog. Rows are shared by every tenant, so these go through get_platform_admin and an unscoped
connection; the user-facing read is GET /v1/catalog/models (app/modules/user/catalog), which lists enabled rows only."""
import uuid

from fastapi import APIRouter, Depends, Query

from app.core.auth import get_platform_admin
from app.core.enums import CatalogStatus
from app.modules.admin.model_catalog import service
from app.modules.admin.model_catalog.schemas import AdminCatalogModel, CatalogModelCreate, CatalogModelUpdate

router = APIRouter(prefix="/v1/admin/model-catalog", tags=["admin"])


@router.get("", response_model=list[AdminCatalogModel], dependencies=[Depends(get_platform_admin)])
async def list_models(
    task: str | None = Query(default=None, description="a task type slug"),
    status: CatalogStatus | None = Query(default=None),
):
    return await service.list_models(task, status)


@router.post("", response_model=AdminCatalogModel, status_code=201)
async def create_model(body: CatalogModelCreate, actor: str = Depends(get_platform_admin)):
    """Admit a model: its task must exist and its licence be on the allowlist (the Hub's, when the Hub is configured)."""
    return await service.create(body, actor)


@router.get("/{model_id}", response_model=AdminCatalogModel, dependencies=[Depends(get_platform_admin)])
async def get_model(model_id: uuid.UUID):
    return await service.get_model(model_id)


@router.patch("/{model_id}", response_model=AdminCatalogModel)
async def update_model(model_id: uuid.UUID, body: CatalogModelUpdate, actor: str = Depends(get_platform_admin)):
    return await service.update(model_id, body, actor)


@router.post("/{model_id}/enable", response_model=AdminCatalogModel)
async def enable_model(model_id: uuid.UUID, actor: str = Depends(get_platform_admin)):
    return await service.set_status(model_id, CatalogStatus.ENABLED, actor)


@router.post("/{model_id}/disable", response_model=AdminCatalogModel)
async def disable_model(model_id: uuid.UUID, actor: str = Depends(get_platform_admin)):
    return await service.set_status(model_id, CatalogStatus.DISABLED, actor)


@router.delete("/{model_id}", status_code=204)
async def delete_model(model_id: uuid.UUID, actor: str = Depends(get_platform_admin)):
    """Only an entry no training preset pins; anything else is a 409 and should be disabled instead."""
    await service.delete(model_id, actor)
