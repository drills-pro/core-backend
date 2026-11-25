"""HTTP surface for model versions: list, publish flags and download. Versions are made by training's settle, never by a request."""
import uuid

from fastapi import APIRouter, Depends
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from app.core.authz import Action, AuthzContext, require
from app.modules.user.model_registry import service
from app.modules.user.model_registry.constants import DOWNLOAD_MEDIA_TYPES, DownloadFile
from app.modules.user.model_registry.schemas import ModelVersionItem

router = APIRouter(prefix="/v1/projects/{project_id}/models", tags=["models"])


@router.get("", response_model=list[ModelVersionItem])
async def list_models(project_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.MODEL_READ))):
    return await service.list_versions(ctx, project_id)


@router.get("/{model_version_id}/download/{file}", response_class=StreamingResponse)
async def download_model_file(
    project_id: uuid.UUID,
    model_version_id: uuid.UUID,
    file: DownloadFile,
    ctx: AuthzContext = Depends(require(Action.MODEL_READ)),
):
    """The version's `model.onnx` or `model.json`, streamed through core-api to a signed-in project member (audited)."""
    client, resp = await service.open_download(ctx, project_id, model_version_id, file)

    async def close():
        await resp.aclose()
        await client.aclose()

    return StreamingResponse(resp.aiter_bytes(), media_type=DOWNLOAD_MEDIA_TYPES[file], background=BackgroundTask(close))
