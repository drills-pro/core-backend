"""What other contexts may ask of projects. Each reader takes the caller's connection, so it is one more query in
the caller's transaction, under the same RLS."""
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.enums import TaskType

_TASK_TYPE = "SELECT task_type FROM projects WHERE id = $1"
_SETTINGS = "SELECT settings FROM projects WHERE id = $1"
_TEAM = "SELECT team_id FROM projects WHERE id = $1"
_OWNED = "SELECT 1 FROM projects WHERE id = $1 AND tenant_id = $2"


async def get_task_type(conn, project_id: uuid.UUID) -> TaskType | None:
    """The project's task type; None when it has none (or the project is not visible to the caller)."""
    value = await conn.fetchval(_TASK_TYPE, project_id)
    return None if value is None else TaskType(value)


async def get_settings(conn, project_id: uuid.UUID) -> dict[str, Any]:
    """The project's free-form settings bag (keys are named by the contexts that read them)."""
    return await conn.fetchval(_SETTINGS, project_id) or {}


async def owns_project(conn, tenant_id: uuid.UUID, project_id: uuid.UUID):
    """The gateway's ownership check (data_plane.access)."""
    return await conn.fetchval(_OWNED, project_id, tenant_id)


async def get_team_id(conn, project_id: uuid.UUID) -> uuid.UUID | None:
    """The team the project belongs to; None when it has none (or the project is not visible to the caller)."""
    return await conn.fetchval(_TEAM, project_id)


# Datasets import projects (task types), so projects cannot import datasets back: the datasets package registers
# its "is this dataset one of the project's" check here when it is imported, and an unregistered check fails closed.
DatasetCheck = Callable[[Any, uuid.UUID, uuid.UUID], Awaitable[bool]]
_dataset_check: DatasetCheck | None = None


def register_dataset_check(check: DatasetCheck) -> None:
    global _dataset_check
    _dataset_check = check


async def has_dataset(conn, project_id: uuid.UUID, dataset_id: uuid.UUID) -> bool:
    if _dataset_check is None:
        raise RuntimeError("no dataset check is registered")
    return await _dataset_check(conn, project_id, dataset_id)
