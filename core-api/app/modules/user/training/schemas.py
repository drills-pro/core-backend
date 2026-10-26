"""Response shapes for the training-run routes."""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class TrainingRunSummary(BaseModel):
    id: uuid.UUID
    status: str
    dataset_ref: str
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
    metrics: Any
    extra_metrics: Any


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
