"""Platform-wide training presets. Rows are shared by every tenant, so these go through get_platform_admin and an
unscoped connection; the user-facing read is GET /v1/projects/{id}/training-presets (app/modules/user/training)."""
import logging

from fastapi import APIRouter, Depends, Path

from app.core import presets, task_types
from app.core.auth import get_platform_admin
from app.core.db import get_pool
from app.core.enums import TaskType
from app.core.presets import NAME_PATTERN, PresetBody, TaskPreset
from app.core.problem import Problem, not_found

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/training-presets", tags=["admin"])


async def _trainable(conn, task: TaskType) -> TaskType:
    info = await task_types.get(conn, task)
    if info is None or not info.trainable:
        raise Problem(422, "task_type_not_trainable", f"task type '{task}' has no trainer, so it has no presets")
    return task


@router.get("/{task}", response_model=list[TaskPreset])
async def list_presets(task: TaskType, actor: str = Depends(get_platform_admin)):
    async with (await get_pool()).acquire() as conn:
        return await presets.list_for(conn, await _trainable(conn, task))


@router.put("/{task}/{name}", response_model=TaskPreset)
async def put_preset(task: TaskType, body: PresetBody, name: str = Path(pattern=NAME_PATTERN), actor: str = Depends(get_platform_admin)):
    """Create or replace one preset. Runs already started keep the values they were given."""
    async with (await get_pool()).acquire() as conn:
        row = await presets.put(conn, await _trainable(conn, task), name, body, actor)
    logger.info("training preset %s/%s set by %s", task, name, actor)
    return row


@router.delete("/{task}/{name}", status_code=204)
async def delete_preset(task: TaskType, name: str = Path(pattern=NAME_PATTERN), actor: str = Depends(get_platform_admin)):
    async with (await get_pool()).acquire() as conn:
        if not await presets.delete(conn, await _trainable(conn, task), name):
            raise not_found()
    logger.info("training preset %s/%s deleted by %s", task, name, actor)
