import uuid

from fastapi import APIRouter, Depends

from app.core.authz import Action, AuthzContext, require
from app.modules.user.webhooks import service
from app.modules.user.webhooks.schemas import CreatedWebhook, NewWebhook, Webhook

router = APIRouter(prefix="/v1/projects/{project_id}/webhooks", tags=["webhooks"])


@router.post("", status_code=201, response_model=CreatedWebhook)
async def create_webhook(project_id: uuid.UUID, body: NewWebhook, ctx: AuthzContext = Depends(require(Action.API_KEY_MANAGE))):
    """Sends the project's job events (see /events) to the url: signed, in order, at least once. Hears new events only."""
    return await service.create(ctx, project_id, body)


@router.get("", response_model=list[Webhook])
async def list_webhooks(project_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.API_KEY_MANAGE))):
    return await service.list_webhooks(ctx, project_id)


@router.delete("/{webhook_id}", status_code=204)
async def delete_webhook(project_id: uuid.UUID, webhook_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.API_KEY_MANAGE))):
    await service.delete(ctx, project_id, webhook_id)
