"""Response shapes for the training-run routes."""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import TaskType, TrainPreset
from app.integrations.job_spec import DEFAULT_SEED, Hyperparameters
from app.modules.user.training.constants import SEED_MAX


class TrainingRunCreate(BaseModel):
    """Start a run on one version of one of the project's datasets. The task comes from the project; naming it is optional."""

    model_config = ConfigDict(extra="forbid")

    dataset_version_id: uuid.UUID
    preset: TrainPreset
    task_type: TaskType | None = None
    hyperparameters: Hyperparameters = Field(default_factory=Hyperparameters)  # a field left out keeps the platform default
    seed: int = Field(default=DEFAULT_SEED, ge=0, le=SEED_MAX)


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
    test_dataset_id: uuid.UUID | None
    test_dataset_version: str | None
    mlflow_run_id: str | None
    reproduction_group_id: uuid.UUID | None
    config: Any
    created_at: datetime


class LineageModel(BaseModel):
    id: uuid.UUID
    mlflow_model_name: str
    mlflow_version: str
    stage: str


class LineageDeployment(BaseModel):
    id: uuid.UUID
    status: str
    route_prefix: str


class TrainingRunLineage(BaseModel):
    training_run: LineageRun
    dataset_commit_message: str | None
    model_version: LineageModel | None
    deployment: LineageDeployment | None
