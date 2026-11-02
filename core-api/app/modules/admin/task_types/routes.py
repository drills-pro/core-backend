"""Platform-wide task types. Rows are shared by every tenant, so these go through get_platform_admin and an
unscoped connection; the rest of the platform reads them through app/core/task_types.py."""
import logging

from fastapi import APIRouter, Depends

from app.core import task_types
from app.core.auth import get_platform_admin
from app.core.db import get_pool
from app.core.enums import TaskType
from app.core.task_types import TaskInfo, TaskTypeBody

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/task-types", tags=["admin"])


@router.get("", response_model=list[TaskInfo])
async def list_task_types(actor: str = Depends(get_platform_admin)):
    async with (await get_pool()).acquire() as conn:
        return await task_types.list_all(conn)


@router.put("/{task}", response_model=TaskInfo)
async def put_task_type(task: TaskType, body: TaskTypeBody, actor: str = Depends(get_platform_admin)):
    """Create or replace one task type. A trainer must already ship in the stable ray_training package."""
    async with (await get_pool()).acquire() as conn:
        row = await task_types.put(conn, task, body, actor)
    logger.info("task type %s set by %s", task, actor)
    return row
