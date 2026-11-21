"""What a training preset trains with, one `task_presets` row per (task, name): the pinned base model, learning rate,
preprocessing, the resources the job declares and the hyperparameter defaults and bounds (docs/trainer-plan.md T5/T6).

Rows are platform data, written only by /v1/admin/training-presets. A run copies the values it used into its job spec, so
editing a row never changes a past run.
"""
from datetime import datetime
from typing import Any

from pydantic import Field

from app.core.enums import TaskType
from app.core.recipe import HyperparameterLimits, Recipe
from app.core.task_catalog import Preprocessing, TextPreprocessing, TrainBaseModel

NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,31}$"  # mirrors task_presets_name_check


class PresetBody(Recipe):
    """What an admin writes for one preset."""

    base_model: TrainBaseModel
    preprocessing: Preprocessing | TextPreprocessing
    hyperparameters: HyperparameterLimits


class TaskPreset(PresetBody):
    """A row: an admin's preset (PresetBody), or one a promoted task bundle wrote (planning/25), which may have no base model
    or preprocessing (tabular) and whose `hyperparameters` are default values its bundle's JSON Schema bounds."""

    task: TaskType
    name: str
    base_model: TrainBaseModel | None = None
    preprocessing: Preprocessing | TextPreprocessing | None = None
    hyperparameters: HyperparameterLimits | dict[str, Any] = Field(union_mode="left_to_right")
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
    repo, revision, license_ = r.pop("base_model_repo"), r.pop("base_model_revision"), r.pop("base_model_license", None)
    r["base_model"] = None if repo is None else {"repo_id": repo, "revision": revision, "license": license_}
    r["preprocessing"] = r["preprocessing"] or None  # {}: a task that transforms nothing (tabular)
    return TaskPreset.model_validate(r)


async def get(conn, task: TaskType, name: str) -> TaskPreset | None:
    row = await conn.fetchrow(f"{_SELECT} WHERE task = $1 AND name = $2", task, name)
    return None if row is None else _model(row)


async def list_for(conn, task: TaskType) -> list[TaskPreset]:
    return [_model(r) for r in await conn.fetch(f"{_SELECT} WHERE task = $1 ORDER BY name", task)]


async def put(conn, task: TaskType, name: str, body: Recipe, actor: str) -> TaskPreset:
    """Upsert one row from an admin's PresetBody or a bundle's ManifestPreset (both a Recipe; the bundle's may lack a base
    model and preprocessing, and its hyperparameters are a dict of defaults)."""
    base, hp = body.base_model, body.hyperparameters
    row = await conn.fetchrow(
        _UPSERT, task, name, base and base.repo_id, base and base.revision, body.learning_rate,
        body.preprocessing.model_dump(mode="json") if body.preprocessing else {}, body.cpu, body.memory_bytes, body.gpu, body.max_runtime_s,
        hp if isinstance(hp, dict) else hp.model_dump(mode="json"), actor,
        body.lr_schedule, body.warmup_fraction, body.augmentation, body.early_stopping_patience, body.vram_gb, base and base.license)
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
