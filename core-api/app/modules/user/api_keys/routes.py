import uuid

from fastapi import APIRouter, Depends

from app.core.authz import Action, AuthzContext, require
from app.modules.user.api_keys import service
from app.modules.user.api_keys.schemas import CreatedApiKey, ListedApiKey, NewApiKey

router = APIRouter(prefix="/v1/projects/{project_id}/api-keys", tags=["api-keys"])


@router.post("", status_code=201, response_model=CreatedApiKey)
async def create_api_key(
    project_id: uuid.UUID, body: NewApiKey, ctx: AuthzContext = Depends(require(Action.API_KEY_MANAGE))
):
    """The secret is in this response only; the database keeps its hash."""
    return await service.create(ctx, project_id, body)


@router.get("", response_model=list[ListedApiKey])
async def list_api_keys(project_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.API_KEY_MANAGE))):
    return await service.list_keys(ctx, project_id)


@router.delete("/{key_id}", status_code=204)
async def revoke_api_key(
    project_id: uuid.UUID, key_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.API_KEY_MANAGE))
):
    """Takes effect on the key's next request. Revoking twice keeps the first revocation time."""
    await service.revoke(ctx, project_id, key_id)
