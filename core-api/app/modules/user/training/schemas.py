"""Response shapes for the training-run routes."""
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.core.enums import TaskType
from app.core.presets import NAME_PATTERN, HyperparameterLimits
from app.core.task_catalog import Augmentation, LrSchedule, TrainBaseModel
from app.integrations.job_spec import DEFAULT_SEED, MAX_EPOCHS
from app.modules.user.training.constants import (
    MAX_EVENT_METRICS,
    MAX_METRIC_NAME_LENGTH,
    SEED_MAX,
    EventType,
)


class HyperparameterOverrides(BaseModel):
    """What a caller changes; a field left out keeps the preset's default, and the preset's bounds apply."""

    epochs: int | None = Field(default=None, ge=1)
    batch_size: int | None = Field(default=None, ge=1)
    image_size: int | None = Field(default=None, ge=1)


class TrainingRunCreate(BaseModel):
    """Start a run on one version of one of the project's datasets. The task comes from the project; naming it is optional."""

    model_config = ConfigDict(extra="forbid")

    dataset_version_id: uuid.UUID
    preset: str = Field(pattern=NAME_PATTERN)  # a name from GET /v1/projects/{id}/training-presets
    task_type: TaskType | None = None
    hyperparameters: HyperparameterOverrides = Field(default_factory=HyperparameterOverrides)
    seed: int = Field(default=DEFAULT_SEED, ge=0, le=SEED_MAX)


class PresetSummary(BaseModel):
    name: str
    base_model: TrainBaseModel
    learning_rate: float
    cpu: float
    memory_bytes: int
    gpu: float
    max_runtime_s: int
    hyperparameters: HyperparameterLimits
    lr_schedule: LrSchedule
    warmup_fraction: float
    augmentation: Augmentation
    early_stopping_patience: int


class TrainingRunSummary(BaseModel):
    id: uuid.UUID
    status: str
    dataset_ref: str
    dataset_version_id: uuid.UUID | None
    preset: str | None
    ray_job_id: str
    mlflow_run_id: str | None
    config: Any
    metrics: Any
    created_at: datetime
    completed_at: datetime | None


MetricName = Annotated[str, StringConstraints(min_length=1, max_length=MAX_METRIC_NAME_LENGTH)]
MetricValue = Annotated[float, Field(allow_inf_nan=False)]


class ProgressMetrics(BaseModel):
    """What a trainer reports after an epoch, as stored and as shown."""

    epoch: int = Field(ge=1, le=MAX_EPOCHS)
    epochs: int = Field(ge=1, le=MAX_EPOCHS)
    metrics: dict[MetricName, MetricValue] = Field(max_length=MAX_EVENT_METRICS)

    @model_validator(mode="after")
    def _epoch_within_epochs(self):
        if self.epoch > self.epochs:
            raise ValueError("epoch is past epochs")
        return self


class ProgressEvent(ProgressMetrics):
    """The body of POST /internal/v1/training-runs/{run_id}/events. Untrusted: every field is bounded. `seq` is unique per
    run (a repeat is a no-op) and below MAX_EPOCHS, which is also the most rows a run can have."""

    model_config = ConfigDict(extra="forbid")

    type: Literal[EventType.PROGRESS]
    seq: int = Field(ge=0, lt=MAX_EPOCHS)


class EventAck(BaseModel):
    seq: int
    duplicate: bool


class ProgressPoint(ProgressMetrics):
    seq: int
    created_at: datetime


class RunProgress(BaseModel):
    """What the run has reported so far; kept after the run ends."""

    latest: ProgressPoint
    history: list[ProgressPoint]  # one per reported epoch, oldest first


class TrainingRun(TrainingRunSummary):
    """Every training_runs column (the detail and cancel responses)."""

    tenant_id: uuid.UUID
    project_id: uuid.UUID
    task: str | None
    report: Any  # the job's report.json (data_plane.train_report.TrainReport); `spec` carries presigned URLs and is never returned
    idempotency_key: str
    model_artifact: Any
    dataset_version: str | None
    test_dataset_id: uuid.UUID | None
    test_dataset_version: str | None
    reproduction_group_id: uuid.UUID | None
    dataset_id: uuid.UUID | None
    progress: RunProgress | None = None  # filled by the detail read only; null until the trainer reports an epoch


class RunLogRef(BaseModel):
    ray_job_id: str
    status: str


class TrainingRunLogs(BaseModel):
    status: str
    logs: str


class LineageRun(BaseModel):
    id: uuid.UUID
    status: str
    dataset_id: uuid.UUID | None
    dataset_version: str | None
    dataset_version_id: uuid.UUID | None
    test_dataset_id: uuid.UUID | None
    test_dataset_version: str | None
    mlflow_run_id: str | None
    reproduction_group_id: uuid.UUID | None
    config: Any
    created_at: datetime
    task: str | None = None
    preset: str | None = None
    metrics: Any = None
    base_model: TrainBaseModel | None = None  # the checkpoint frozen into the run's spec; None for runs from before it was recorded
