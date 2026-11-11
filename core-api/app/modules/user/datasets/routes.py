"""Datasets HTTP surface: parse, authorize, call the service, shape the response."""
import uuid

from fastapi import APIRouter, Depends, Query

from app.core.authz import Action, AuthzContext, require
from app.core.idempotency import IdempotencyKey
from app.core.pagination import Page, PageParams
from app.modules.user.datasets import import_service, preview_service, service
from app.modules.user.datasets.constants import PREVIEW_DEFAULT_LIMIT, PREVIEW_MAX_LIMIT
from app.modules.user.datasets.schemas import (
    DatasetCreate,
    DatasetImport,
    DatasetPreview,
    DatasetSummary,
    DatasetVersion,
    FileLink,
    ImportCreate,
    PublishRequest,
)

router = APIRouter(prefix="/v1/projects/{project_id}/datasets", tags=["datasets"])


@router.post("", status_code=201, response_model=DatasetSummary)
async def create_dataset(
    project_id: uuid.UUID, body: DatasetCreate, ctx: AuthzContext = Depends(require(Action.DATASET_WRITE))
):
    return await service.create_dataset(ctx, project_id, body.name, body.data_categories)


@router.get("", response_model=Page[DatasetSummary])
async def list_datasets(
    project_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.DATASET_READ)), page: PageParams = Depends()
):
    return await service.list_datasets(ctx, project_id, page)


@router.get("/{dataset_id}", response_model=DatasetSummary)
async def get_dataset(
    project_id: uuid.UUID, dataset_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.DATASET_READ))
):
    return await service.get_dataset(ctx, project_id, dataset_id)


@router.delete("/{dataset_id}", status_code=202, response_model=DatasetSummary)
async def delete_dataset(
    project_id: uuid.UUID, dataset_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.DATASET_WRITE))
):
    return await service.delete_dataset(ctx, project_id, dataset_id)


@router.post("/{dataset_id}:restore", response_model=DatasetSummary)
async def restore_dataset(
    project_id: uuid.UUID, dataset_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.DATASET_WRITE))
):
    return await service.restore_dataset(ctx, project_id, dataset_id)


@router.put("/{dataset_id}/published", response_model=DatasetSummary)
async def publish_version(
    project_id: uuid.UUID, dataset_id: uuid.UUID, body: PublishRequest, ctx: AuthzContext = Depends(require(Action.DATASET_WRITE))
):
    return await service.publish_version(ctx, project_id, dataset_id, body.version_id)


@router.delete("/{dataset_id}/published", response_model=DatasetSummary)
async def unpublish_dataset(
    project_id: uuid.UUID, dataset_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.DATASET_WRITE))
):
    return await service.unpublish_dataset(ctx, project_id, dataset_id)


@router.get("/{dataset_id}/versions", response_model=Page[DatasetVersion])
async def list_versions(
    project_id: uuid.UUID,
    dataset_id: uuid.UUID,
    ctx: AuthzContext = Depends(require(Action.DATASET_READ)),
    page: PageParams = Depends(),
):
    return await service.list_versions(ctx, project_id, dataset_id, page)


@router.post("/{dataset_id}/imports", status_code=201, response_model=DatasetImport)
async def create_import(
    project_id: uuid.UUID,
    dataset_id: uuid.UUID,
    body: ImportCreate,
    idempotency_key: IdempotencyKey,
    ctx: AuthzContext = Depends(require(Action.DATASET_WRITE)),
):
    return await import_service.create_import(ctx, project_id, dataset_id, body, idempotency_key)


@router.get("/{dataset_id}/imports", response_model=Page[DatasetImport])
async def list_imports(
    project_id: uuid.UUID,
    dataset_id: uuid.UUID,
    ctx: AuthzContext = Depends(require(Action.DATASET_READ)),
    page: PageParams = Depends(),
):
    return await import_service.list_imports(ctx, project_id, dataset_id, page)


@router.get("/{dataset_id}/imports/{import_id}", response_model=DatasetImport)
async def get_import(
    project_id: uuid.UUID, dataset_id: uuid.UUID, import_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.DATASET_READ))
):
    return await import_service.get_import(ctx, project_id, dataset_id, import_id)


@router.post("/{dataset_id}/imports/{import_id}/start", status_code=202, response_model=DatasetImport)
async def start_import(
    project_id: uuid.UUID, dataset_id: uuid.UUID, import_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.DATASET_WRITE))
):
    return await import_service.start_import(ctx, project_id, dataset_id, import_id)


@router.post("/{dataset_id}/imports/{import_id}/cancel", response_model=DatasetImport)
async def cancel_import(
    project_id: uuid.UUID, dataset_id: uuid.UUID, import_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.DATASET_WRITE))
):
    return await import_service.cancel_import(ctx, project_id, dataset_id, import_id)


@router.post("/{dataset_id}/imports/{import_id}/commit", response_model=DatasetVersion)
async def commit_import(
    project_id: uuid.UUID, dataset_id: uuid.UUID, import_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.DATASET_WRITE))
):
    return await import_service.commit_import(ctx, project_id, dataset_id, import_id)


@router.post("/{dataset_id}/imports/{import_id}/discard", response_model=DatasetImport)
async def discard_import(
    project_id: uuid.UUID, dataset_id: uuid.UUID, import_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.DATASET_WRITE))
):
    return await import_service.discard_import(ctx, project_id, dataset_id, import_id)


_Limit = Query(PREVIEW_DEFAULT_LIMIT, ge=1, le=PREVIEW_MAX_LIMIT)


@router.get("/{dataset_id}/versions/{version_id}/preview", response_model=DatasetPreview)
async def preview_version(
    project_id: uuid.UUID,
    dataset_id: uuid.UUID,
    version_id: uuid.UUID,
    limit: int = _Limit,
    ctx: AuthzContext = Depends(require(Action.DATASET_READ)),
):
    return await preview_service.version_preview(ctx, project_id, dataset_id, version_id, limit)


@router.get("/{dataset_id}/versions/{version_id}/files/{path:path}", response_model=FileLink)
async def version_file(
    project_id: uuid.UUID,
    dataset_id: uuid.UUID,
    version_id: uuid.UUID,
    path: str,
    ctx: AuthzContext = Depends(require(Action.DATASET_READ)),
):
    return await preview_service.file_link(ctx, project_id, dataset_id, version_id, path)


@router.get("/{dataset_id}/imports/{import_id}/preview", response_model=DatasetPreview)
async def preview_import(
    project_id: uuid.UUID,
    dataset_id: uuid.UUID,
    import_id: uuid.UUID,
    limit: int = _Limit,
    ctx: AuthzContext = Depends(require(Action.DATASET_READ)),
):
    return await preview_service.import_preview(ctx, project_id, dataset_id, import_id, limit)
