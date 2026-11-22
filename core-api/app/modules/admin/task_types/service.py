"""What an admin may do to the task types: one transaction per write, with its admin_audit row."""
from http import HTTPStatus

from app.core import admin_audit, task_types
from app.core.enums import AdminAuditAction, AdminAuditEntity, TaskType
from app.core.problem import Problem
from app.core.task_types import TaskInfo, TaskTypeBody
from app.integrations import data_plane
from app.modules.admin.task_types.constants import ProblemCode


async def put(conn, task: TaskType, body: TaskTypeBody, actor: str) -> TaskInfo:
    """Create or replace one task type. A trainer must already ship in the stable ray_training package; the serving
    functions must exist in the serve files this build sends to the replica."""
    if missing := data_plane.unknown_serving_functions(body.encoder, body.postprocess):
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.UNKNOWN_SERVING_FUNCTION, f"no such serving function: {', '.join(missing)}")
    async with conn.transaction():
        row = await task_types.put(conn, task, body, actor)
        await admin_audit.record(conn, actor, AdminAuditAction.TASK_TYPE_PUT, AdminAuditEntity.TASK_TYPE, task, body.model_dump(mode="json"))
    return row
