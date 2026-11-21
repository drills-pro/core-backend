"""Platform-wide training presets. Rows are shared by every tenant, so these go through get_platform_admin and an
unscoped connection; the user-facing read is GET /v1/projects/{id}/training-presets (app/modules/user/training)."""
import logging

from fastapi import APIRouter, Depends, Path

from app.core.auth import get_platform_admin
from app.core.db import get_pool
from app.core.enums import TaskType
from app.core.presets import NAME_PATTERN, PresetBody, TaskPreset
from app.modules.admin.presets import service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/training-presets", tags=["admin"])


@router.get("/{task}", response_model=list[TaskPreset])
async def list_presets(task: TaskType, actor: str = Depends(get_platform_admin)):
    async with (await get_pool()).acquire() as conn:
        return await service.list_for(conn, task)


@router.put("/{task}/{name}", response_model=TaskPreset)
async def put_preset(task: TaskType, body: PresetBody, name: str = Path(pattern=NAME_PATTERN), actor: str = Depends(get_platform_admin)):
    """Create or replace one preset. Runs already started keep the values they were given."""
    body = await service.admitted(task, body)
    async with (await get_pool()).acquire() as conn:
        row = await service.put(conn, task, name, body, actor)
    logger.info("training preset %s/%s set by %s", task, name, actor)
    return row


@router.delete("/{task}/{name}", status_code=204)
async def delete_preset(task: TaskType, name: str = Path(pattern=NAME_PATTERN), actor: str = Depends(get_platform_admin)):
    async with (await get_pool()).acquire() as conn:
        await service.delete(conn, task, name, actor)
    logger.info("training preset %s/%s deleted by %s", task, name, actor)
