"""Platform-wide task types. Rows are shared by every tenant, so these go through get_platform_admin and an
unscoped connection; the rest of the platform reads them through app/core/task_types.py."""
import logging

from http import HTTPStatus

from fastapi import APIRouter, Depends

from app.core import task_types
from app.core.auth import get_platform_admin
from app.core.db import get_pool
from app.core.enums import TaskType
from app.core.problem import Problem
from app.core.task_types import TaskInfo, TaskTypeBody
from app.integrations import serve_client

logger = logging.getLogger(__name__)

UNKNOWN_SERVING_FUNCTION = "unknown_serving_function"

router = APIRouter(prefix="/v1/admin/task-types", tags=["admin"])


@router.get("", response_model=list[TaskInfo])
async def list_task_types(actor: str = Depends(get_platform_admin)):
    async with (await get_pool()).acquire() as conn:
        return await task_types.list_all(conn)


@router.put("/{task}", response_model=TaskInfo)
async def put_task_type(task: TaskType, body: TaskTypeBody, actor: str = Depends(get_platform_admin)):
    """Create or replace one task type. A trainer must already ship in the stable ray_training package; the serving
    functions must exist in the serve files this build sends to the replica."""
    if missing := serve_client.unknown_serving_functions(body.encoder, body.postprocess):
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, UNKNOWN_SERVING_FUNCTION, f"no such serving function: {', '.join(missing)}")
    async with (await get_pool()).acquire() as conn:
        row = await task_types.put(conn, task, body, actor)
    logger.info("task type %s set by %s", task, actor)
    return row
