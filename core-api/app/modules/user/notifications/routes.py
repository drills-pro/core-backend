import uuid

from fastapi import APIRouter, Depends

from app.core.authz import Action, AuthzContext, require
from app.modules.user.notifications import service
from app.modules.user.notifications.schemas import MarkRead, Notifications

router = APIRouter(prefix="/v1/projects/{project_id}/notifications", tags=["notifications"])


@router.get("", response_model=Notifications)
async def list_notifications(project_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.JOB_READ))):
    """The project's job events the caller has not marked read, newest first, with the unread count."""
    return await service.list_unread(ctx, project_id)


@router.post("/read", status_code=204)
async def mark_notifications_read(project_id: uuid.UUID, body: MarkRead, ctx: AuthzContext = Depends(require(Action.JOB_READ))):
    """Moves the caller's cursor to `seq` (never backwards); events up to it stop counting as unread."""
    await service.mark_read(ctx, project_id, body.seq)
