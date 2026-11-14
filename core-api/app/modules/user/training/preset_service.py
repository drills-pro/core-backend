"""The presets a project's task can train with."""
import uuid

from app.core import presets
from app.core.authz import AuthzContext
from app.core.enums import TaskType
from app.core.presets import TaskPreset
from app.core.tenant_conn import TenantConn
from app.modules.user.projects import get_task_type


async def list_for_project(ctx: AuthzContext, project_id: uuid.UUID) -> list[TaskPreset]:
    async with TenantConn(ctx.tenant_id) as conn:
        task = await get_task_type(conn, project_id)
        return [] if task is None else [p for p in await presets.list_for(conn, TaskType(task)) if p.enabled]
