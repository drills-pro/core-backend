"""The training recipe, declared once: what a preset (an admin's `PresetBody`, a bundle's `ManifestPreset`, the `TaskPreset`
row) trains with and the resources its job declares. A run copies these values into its job spec (`values_for`), so editing
a preset never changes a past run.
"""
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.enums import TaskType
from app.core.settings import settings
from app.core.task_catalog import TASK_AUGMENTATIONS, Augmentation, LrSchedule, Preprocessing, TextPreprocessing, TrainBaseModel

REVISION = re.compile(r"^[0-9a-f]{40}$")  # a commit sha, never a branch that moves


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


class Recipe(BaseModel):
    """The fields every preset shape shares. A subclass says which of `base_model`, `preprocessing` and `hyperparameters` it
    requires (a task that trains from scratch has no base model and no preprocessing)."""

    model_config = ConfigDict(extra="forbid")

    base_model: TrainBaseModel | None = None
    learning_rate: float = Field(gt=0)
    preprocessing: Preprocessing | TextPreprocessing | None = None
    cpu: float = Field(gt=0)
    memory_bytes: int = Field(gt=0)
    gpu: float = Field(ge=0)
    vram_gb: float | None = Field(default=None, gt=0)  # GPU memory the run may hold, in GiB; the trainer caps itself at it
    max_runtime_s: int = Field(gt=0)
    hyperparameters: HyperparameterLimits | dict[str, Any]
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
        if self.base_model is not None and not REVISION.match(self.base_model.revision):
            raise ValueError("base_model.revision must be a 40-character commit sha")
        if self.max_runtime_s > settings.training.max_runtime_seconds:  # presigned data reads are sized to the ceiling
            raise ValueError(f"max_runtime_s is at most {settings.training.max_runtime_seconds}")
        return self

    def values_for(self, model: type[BaseModel], exclude: frozenset[str] = frozenset()) -> dict[str, Any]:
        """This recipe's values for the fields `model` also declares, as the objects themselves: how a job spec copies the
        recipe, so a field added here reaches it by name."""
        return {name: getattr(self, name) for name in model.model_fields if name in Recipe.model_fields and name not in exclude}
