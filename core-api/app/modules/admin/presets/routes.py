"""Platform-wide training presets. Rows are shared by every tenant, so these go through get_platform_admin and an
unscoped connection; the user-facing read is GET /v1/projects/{id}/training-presets (app/modules/user/training)."""
import logging
from http import HTTPStatus

from fastapi import APIRouter, Depends, Path

from app.core import admin_audit, presets, task_types
from app.core.settings import settings
from app.integrations import hub
from app.core.auth import get_platform_admin
from app.core.db import get_pool
from app.core.enums import AdminAuditAction, AdminAuditEntity, TaskType
from app.core.presets import NAME_PATTERN, PresetBody, TaskPreset
from app.core.problem import Problem, not_found

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/training-presets", tags=["admin"])

TASK_NOT_TRAINABLE = "task_type_not_trainable"
INVALID_RECIPE = "invalid_training_recipe"
MODEL_NOT_ADMITTED = "model_not_admitted"
HUB_UNAVAILABLE = "hub_unavailable"


async def _trainable(conn, task: TaskType) -> TaskType:
    info = await task_types.get(conn, task)
    if info is None or not info.trainable:
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, TASK_NOT_TRAINABLE, f"task type '{task}' has no trainer, so it has no presets")
    return task


async def _admit(body: PresetBody) -> PresetBody:
    """Check the pinned base model against the admission policy and record the licence the Hub declares for it."""
    base = body.base_model
    try:
        facts = await hub.fetch(base.repo_id, base.revision)
    except hub.HubUnavailable as e:
        raise Problem(HTTPStatus.BAD_GATEWAY, HUB_UNAVAILABLE, str(e))
    if reasons := hub.refusals(facts):
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, MODEL_NOT_ADMITTED, f"{base.repo_id}: " + "; ".join(reasons))
    return body.model_copy(update={"base_model": base.model_copy(update={"license": facts.license})})


@router.get("/{task}", response_model=list[TaskPreset])
async def list_presets(task: TaskType, actor: str = Depends(get_platform_admin)):
    async with (await get_pool()).acquire() as conn:
        return await presets.list_for(conn, await _trainable(conn, task))


@router.put("/{task}/{name}", response_model=TaskPreset)
async def put_preset(task: TaskType, body: PresetBody, name: str = Path(pattern=NAME_PATTERN), actor: str = Depends(get_platform_admin)):
    """Create or replace one preset. Runs already started keep the values they were given."""
    try:
        body.check_task(task)
    except ValueError as e:
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, INVALID_RECIPE, str(e))
    if settings.training.hub_url:
        body = await _admit(body)
    async with (await get_pool()).acquire() as conn, conn.transaction():
        row = await presets.put(conn, await _trainable(conn, task), name, body, actor)
        await admin_audit.record(conn, actor, AdminAuditAction.TASK_PRESET_PUT, AdminAuditEntity.TASK_PRESET, f"{task}/{name}", body.model_dump(mode="json"))
    logger.info("training preset %s/%s set by %s", task, name, actor)
    return row


@router.delete("/{task}/{name}", status_code=204)
async def delete_preset(task: TaskType, name: str = Path(pattern=NAME_PATTERN), actor: str = Depends(get_platform_admin)):
    async with (await get_pool()).acquire() as conn, conn.transaction():
        if not await presets.delete(conn, await _trainable(conn, task), name):
            raise not_found()
        await admin_audit.record(conn, actor, AdminAuditAction.TASK_PRESET_DELETE, AdminAuditEntity.TASK_PRESET, f"{task}/{name}")
    logger.info("training preset %s/%s deleted by %s", task, name, actor)
