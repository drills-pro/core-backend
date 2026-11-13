"""What a training preset trains with, one `task_presets` row per (task, name): the pinned base model, learning rate,
preprocessing, the resources the job declares and the hyperparameter defaults and bounds (docs/trainer-plan.md T5/T6).

Rows are platform data, written only by /v1/admin/training-presets. A run copies the values it used into its job spec, so
editing a row never changes a past run.
"""
import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.enums import TaskType
from app.core.settings import settings
from app.core.task_catalog import TASK_AUGMENTATIONS, Augmentation, LrSchedule, Preprocessing, TextPreprocessing, TrainBaseModel

NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,31}$"  # mirrors task_presets_name_check
_REVISION = re.compile(r"^[0-9a-f]{40}$")  # a commit sha, never a branch that moves


class HyperparameterRange(BaseModel):
    default: int
    min: int
    max: int

    @model_validator(mode="after")
    def _ordered(self):
        if not 1 <= self.min <= self.default <= self.max:
            raise ValueError("a range needs 1 <= min <= default <= max")
        return self


class HyperparameterLimits(BaseModel):
    epochs: HyperparameterRange
    batch_size: HyperparameterRange
    image_size: HyperparameterRange


class PresetBody(BaseModel):
    """What an admin writes for one preset."""

    model_config = ConfigDict(extra="forbid")

    base_model: TrainBaseModel
    learning_rate: float = Field(gt=0)
    preprocessing: Preprocessing | TextPreprocessing
    cpu: float = Field(gt=0)
    memory_bytes: int = Field(gt=0)
    gpu: float = Field(ge=0)
    vram_gb: float | None = Field(default=None, gt=0)  # GPU memory the run may hold, in GiB; the trainer caps itself at it
    max_runtime_s: int = Field(gt=0)
    hyperparameters: HyperparameterLimits
    lr_schedule: LrSchedule = LrSchedule.CONSTANT
    warmup_fraction: float = Field(default=0, ge=0, lt=1)  # of the run's steps, linear from 0 to learning_rate
    augmentation: Augmentation = Augmentation.NONE
    early_stopping_patience: int = Field(default=0, ge=0)  # epochs without a better metric before stopping; 0 = off

    def check_task(self, task: TaskType) -> None:
        """The recipe must be one the task's trainer implements."""
        if self.augmentation not in TASK_AUGMENTATIONS.get(task, frozenset({Augmentation.NONE})):
            raise ValueError(f"augmentation {self.augmentation} is not available for {task}")

    @model_validator(mode="after")
    def _pinned_and_within_the_ceiling(self):
        if not _REVISION.match(self.base_model.revision):
            raise ValueError("base_model.revision must be a 40-character commit sha")
        if self.max_runtime_s > settings.training.max_runtime_seconds:  # presigned data reads are sized to the ceiling
            raise ValueError(f"max_runtime_s is at most {settings.training.max_runtime_seconds}")
        return self


class TaskPreset(PresetBody):
    task: TaskType
    name: str
    enabled: bool = True  # a switched-off preset is neither listed to tenants nor accepted for a new run
    updated_by: str
    updated_at: datetime


_SELECT = "SELECT * FROM task_presets"
_UPSERT = (
    "INSERT INTO task_presets (task, name, base_model_repo, base_model_revision, learning_rate, preprocessing, cpu, "
    "memory_bytes, gpu, max_runtime_s, hyperparameters, updated_by, lr_schedule, warmup_fraction, augmentation, "
    "early_stopping_patience, vram_gb, base_model_license) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18) "
    "ON CONFLICT (task, name) DO UPDATE SET base_model_repo = EXCLUDED.base_model_repo, "
    "base_model_revision = EXCLUDED.base_model_revision, learning_rate = EXCLUDED.learning_rate, "
    "preprocessing = EXCLUDED.preprocessing, cpu = EXCLUDED.cpu, memory_bytes = EXCLUDED.memory_bytes, gpu = EXCLUDED.gpu, vram_gb = EXCLUDED.vram_gb, base_model_license = EXCLUDED.base_model_license, "
    "max_runtime_s = EXCLUDED.max_runtime_s, hyperparameters = EXCLUDED.hyperparameters, "
    "lr_schedule = EXCLUDED.lr_schedule, warmup_fraction = EXCLUDED.warmup_fraction, augmentation = EXCLUDED.augmentation, "
    "early_stopping_patience = EXCLUDED.early_stopping_patience, updated_by = EXCLUDED.updated_by, updated_at = now() RETURNING *"
)


def _model(row) -> TaskPreset:
    r = dict(row)
    r["base_model"] = {"repo_id": r.pop("base_model_repo"), "revision": r.pop("base_model_revision"), "license": r.pop("base_model_license", None)}
    return TaskPreset.model_validate(r)


async def get(conn, task: TaskType, name: str) -> TaskPreset | None:
    row = await conn.fetchrow(f"{_SELECT} WHERE task = $1 AND name = $2", task, name)
    return None if row is None else _model(row)


async def list_for(conn, task: TaskType) -> list[TaskPreset]:
    return [_model(r) for r in await conn.fetch(f"{_SELECT} WHERE task = $1 ORDER BY name", task)]


async def put(conn, task: TaskType, name: str, body: PresetBody, actor: str) -> TaskPreset:
    row = await conn.fetchrow(
        _UPSERT, task, name, body.base_model.repo_id, body.base_model.revision, body.learning_rate,
        body.preprocessing.model_dump(mode="json"), body.cpu, body.memory_bytes, body.gpu, body.max_runtime_s,
        body.hyperparameters.model_dump(mode="json"), actor,
        body.lr_schedule, body.warmup_fraction, body.augmentation, body.early_stopping_patience, body.vram_gb, body.base_model.license)
    return _model(row)


async def delete(conn, task: TaskType, name: str) -> bool:
    return await conn.fetchval("DELETE FROM task_presets WHERE task = $1 AND name = $2 RETURNING true", task, name) is True


async def delete_others(conn, task: TaskType, keep: list[str]) -> None:
    """Drop the task's presets not in `keep`: a promoted bundle's manifest is the whole preset list."""
    await conn.execute("DELETE FROM task_presets WHERE task = $1 AND NOT (name = ANY($2::text[]))", task, keep)


async def set_enabled(conn, task: TaskType, name: str, enabled: bool, actor: str) -> TaskPreset | None:
    row = await conn.fetchrow(
        "UPDATE task_presets SET enabled = $3, updated_by = $4, updated_at = now() WHERE task = $1 AND name = $2 RETURNING *",
        task, name, enabled, actor)
    return None if row is None else _model(row)
