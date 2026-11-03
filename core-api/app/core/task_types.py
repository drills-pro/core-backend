"""What the platform can do with each task type, one `task_types` row per slug: the kind of data it works on, the trainer
that trains it (none = not trainable) and whether a deployment can serve its models. Everything that asks "is it
supported / tabular / trainable / servable" asks this module, on the caller's connection, so enabling a task takes effect
on the next request. Platform data, written only through /v1/admin/task-types.
"""
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.enums import TaskType
from app.core.problem import Problem
from app.core.task_catalog import DataKind

TRAINER_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$"  # "module:function", mirrors task_types_trainer_check
IDENTIFIER_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"  # mirrors task_types_encoder_check; the names are formatted into the serve script
TASK_NOT_SUPPORTED = "task_type_not_supported"
DEFAULT_SCORE_THRESHOLD = 0.3  # a (query, class) pair scoring below this is not reported
DEFAULT_MAX_RESULTS = 100
MAX_RESULTS_LIMIT = 1000


class ExecutionProvider(StrEnum):
    """The onnxruntime providers a deployment may ask for."""

    CPU = "CPUExecutionProvider"
    CUDA = "CUDAExecutionProvider"


class ServingParams(BaseModel):
    """The knobs a task's serving functions read; a function ignores the ones it has no use for."""

    model_config = ConfigDict(extra="forbid")

    score_threshold: float = Field(default=DEFAULT_SCORE_THRESHOLD, ge=0, le=1)
    max_results: int = Field(default=DEFAULT_MAX_RESULTS, ge=1, le=MAX_RESULTS_LIMIT)
    providers: list[ExecutionProvider] = Field(default=[ExecutionProvider.CPU], min_length=1)


class TaskTypeBody(BaseModel):
    """What an admin writes for one task type."""

    model_config = ConfigDict(extra="forbid")

    data_kind: DataKind
    trainer: str | None = Field(default=None, pattern=TRAINER_PATTERN)  # a trainer in the ray_training package; None = not trainable
    servable: bool = False
    encoder: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)  # a function in serve_inputs.py
    postprocess: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)  # a function in serve_postprocess.py
    serving: ServingParams = ServingParams()
    enabled: bool = True

    @model_validator(mode="after")
    def _servable_has_functions(self):  # mirrors task_types_servable_check
        if self.servable and not (self.encoder and self.postprocess):
            raise ValueError("a servable task needs an encoder and a postprocess")
        return self


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


_SELECT = "SELECT * FROM task_types"
_UPSERT = (
    "INSERT INTO task_types (slug, data_kind, trainer, servable, encoder, postprocess, serving, enabled, updated_by) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
    "ON CONFLICT (slug) DO UPDATE SET data_kind = EXCLUDED.data_kind, trainer = EXCLUDED.trainer, servable = EXCLUDED.servable, "
    "encoder = EXCLUDED.encoder, postprocess = EXCLUDED.postprocess, serving = EXCLUDED.serving, "
    "enabled = EXCLUDED.enabled, updated_by = EXCLUDED.updated_by, updated_at = now() RETURNING *"
)


async def get(conn, task: TaskType) -> TaskInfo | None:
    row = await conn.fetchrow(f"{_SELECT} WHERE slug = $1", task)
    return None if row is None else TaskInfo.model_validate(dict(row))


async def list_all(conn) -> list[TaskInfo]:
    return [TaskInfo.model_validate(dict(r)) for r in await conn.fetch(f"{_SELECT} ORDER BY slug")]


async def put(conn, task: TaskType, body: TaskTypeBody, actor: str) -> TaskInfo:
    row = await conn.fetchrow(
        _UPSERT, task, body.data_kind, body.trainer, body.servable, body.encoder, body.postprocess,
        body.serving.model_dump(mode="json"), body.enabled, actor)
    return TaskInfo.model_validate(dict(row))


async def require_supported(conn, task: TaskType) -> TaskInfo:
    """The task's row, or a 422 when the platform has none for it or it is switched off."""
    info = await get(conn, task)
    if info is None or not info.enabled:
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, TASK_NOT_SUPPORTED, f"task type '{task}' is not supported yet")
    return info
