"""What the platform can do with each task type, one `task_types` row per slug: the kind of data it works on, the trainer
that trains it (none = not trainable) and whether a deployment can serve its models. Everything that asks "is it
supported / tabular / trainable / servable" asks this module, on the caller's connection, so enabling a task takes effect
on the next request. Platform data, written only through /v1/admin/task-types.
"""
from datetime import datetime
from http import HTTPStatus

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import TaskType
from app.core.problem import Problem
from app.core.task_catalog import DataKind

TRAINER_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$"  # "module:function", mirrors task_types_trainer_check
TASK_NOT_SUPPORTED = "task_type_not_supported"


class TaskTypeBody(BaseModel):
    """What an admin writes for one task type."""

    model_config = ConfigDict(extra="forbid")

    data_kind: DataKind
    trainer: str | None = Field(default=None, pattern=TRAINER_PATTERN)  # a trainer in the ray_training package; None = not trainable
    servable: bool = False
    enabled: bool = True


class TaskInfo(TaskTypeBody):
    slug: TaskType
    updated_by: str
    updated_at: datetime

    @property
    def trainable(self) -> bool:
        return self.trainer is not None

    @property
    def is_tabular(self) -> bool:
        return self.data_kind is DataKind.TABULAR

    @property
    def is_table(self) -> bool:
        """The dataset is a table (data.parquet + schema.json) rather than an image index."""
        return self.data_kind in (DataKind.TABULAR, DataKind.TEXT)


_SELECT = "SELECT * FROM task_types"
_UPSERT = (
    "INSERT INTO task_types (slug, data_kind, trainer, servable, enabled, updated_by) VALUES ($1, $2, $3, $4, $5, $6) "
    "ON CONFLICT (slug) DO UPDATE SET data_kind = EXCLUDED.data_kind, trainer = EXCLUDED.trainer, servable = EXCLUDED.servable, "
    "enabled = EXCLUDED.enabled, updated_by = EXCLUDED.updated_by, updated_at = now() RETURNING *"
)


async def get(conn, task: TaskType) -> TaskInfo | None:
    row = await conn.fetchrow(f"{_SELECT} WHERE slug = $1", task)
    return None if row is None else TaskInfo.model_validate(dict(row))


async def list_all(conn) -> list[TaskInfo]:
    return [TaskInfo.model_validate(dict(r)) for r in await conn.fetch(f"{_SELECT} ORDER BY slug")]


async def put(conn, task: TaskType, body: TaskTypeBody, actor: str) -> TaskInfo:
    row = await conn.fetchrow(_UPSERT, task, body.data_kind, body.trainer, body.servable, body.enabled, actor)
    return TaskInfo.model_validate(dict(row))


async def require_supported(conn, task: TaskType) -> TaskInfo:
    """The task's row, or a 422 when the platform has none for it or it is switched off."""
    info = await get(conn, task)
    if info is None or not info.enabled:
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, TASK_NOT_SUPPORTED, f"task type '{task}' is not supported yet")
    return info
