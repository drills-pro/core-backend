import uuid

from fastapi import APIRouter, Depends, Query

from app.core.authz import Action, AuthzContext, require
from app.modules.user.job_events import service
from app.modules.user.job_events.constants import EVENTS_DEFAULT_LIMIT, EVENTS_MAX_LIMIT
from app.modules.user.job_events.schemas import JobEvent

router = APIRouter(prefix="/v1/projects/{project_id}/events", tags=["events"])


@router.get("", response_model=list[JobEvent])
async def list_events(
    project_id: uuid.UUID,
    after: int = Query(default=0, ge=0, description="the `seq` of the last event already read"),
    limit: int = Query(default=EVENTS_DEFAULT_LIMIT, ge=1, le=EVENTS_MAX_LIMIT),
    ctx: AuthzContext = Depends(require(Action.JOB_READ)),
):
    """State changes of the project's training runs, dataset imports and deployments, oldest first."""
    return await service.list_events(ctx, project_id, after, limit)
