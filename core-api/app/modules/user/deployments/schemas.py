"""Response shapes for the deployment routes."""
import json
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.user.datasets import VersionRef
from app.modules.user.deployments.constants import METADATA_MAX_BYTES, METADATA_TOO_LARGE_DETAIL
from app.modules.user.training import LineageRun

class DeploymentCreated(BaseModel):
    """predict_key is present only when a new deployment was minted (shown once); a 'main' re-deploy omits it."""

    id: uuid.UUID
    status: str
    route_prefix: str
    rate_limit_per_minute: int
    plan_tier: str | None
    predict_key: str | None = None


class DeploymentListItem(BaseModel):
    id: uuid.UUID
    status: str
    route_prefix: str
    created_at: datetime


class DeploymentDetail(DeploymentListItem):
    project_id: uuid.UUID
    training_run_id: uuid.UUID
    rate_limit_per_minute: int


class TestCallResult(BaseModel):
    __test__ = False  # not a pytest class

    response: Any
    latency_ms: float


class TestCall(BaseModel):
    __test__ = False

    id: int
    request: Any
    response: Any
    latency_ms: float | None
    created_at: datetime


class RunDeployment(BaseModel):
    id: uuid.UUID
    status: str
    route_prefix: str


class LineageModel(BaseModel):
    id: uuid.UUID
    mlflow_model_name: str
    mlflow_version: str
    stage: str


class TrainingRunLineage(BaseModel):
    training_run: LineageRun
    dataset_version: VersionRef | None
    model_version: LineageModel | None
    deployment: RunDeployment | None


class PredictRequest(BaseModel):
    """POST /v1/predict body. Closed: an unknown top-level key is a 422. `input` goes to the model as sent."""

    model_config = ConfigDict(extra="forbid")

    input: Any
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _small(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value)) > METADATA_MAX_BYTES:
            raise ValueError(METADATA_TOO_LARGE_DETAIL)
        return value


class PredictModelRef(BaseModel):
    id: uuid.UUID
    revision: str


class PredictUsage(BaseModel):
    model_config = ConfigDict(extra="allow")  # token or compute counts later

    latency_ms: float


class PredictionEnvelope(BaseModel):
    id: str
    model: PredictModelRef
    output: Any
    usage: PredictUsage
    metadata: dict[str, Any]
    error: None = None
    created_at: datetime
