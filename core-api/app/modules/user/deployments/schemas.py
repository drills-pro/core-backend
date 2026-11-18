"""Response shapes for the deployment routes."""
import json
import uuid
from datetime import datetime
from http import HTTPStatus
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.task_catalog import TrainBaseModel
from app.modules.user.datasets import VersionRef
from app.modules.user.deployments.constants import METADATA_MAX_BYTES, METADATA_TOO_LARGE_DETAIL, PredictionStatus
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
    model_version_id: uuid.UUID | None  # the one version this deployment serves; None for a row older than versions


class DeploymentDetail(DeploymentListItem):
    project_id: uuid.UUID
    training_run_id: uuid.UUID
    rate_limit_per_minute: int


class PredictionError(BaseModel):
    code: str
    detail: str


class TestCall(BaseModel):
    """One console prediction of a deployment (source=console): the try-it log is a filtered list of stored predictions."""

    __test__ = False

    id: str  # the prediction id, as POST /v1/predict returned it
    status: PredictionStatus
    request: Any  # {"input": ...} as it was sent; None once the retention purge dropped it
    response: Any  # the output; None for a call that failed or was purged
    error: PredictionError | None = None
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


class ModelCard(BaseModel):
    """The facts a person needs before trusting a model, assembled from its run: what it was trained on, how it was scored, what it is."""

    training_run_id: uuid.UUID
    task: str | None
    preset: str | None
    base_model: TrainBaseModel | None
    near_duplicates: int | None  # a score measured on images the model trained on near-copies of is too good; None = not measured
    metrics: Any
    dataset_version: VersionRef | None
    test_dataset_version: str | None
    has_test_split: bool
    model_version: LineageModel | None


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

    latency_ms: float | None = None  # None until a call finished


class PredictionEnvelope(BaseModel):
    id: str
    status: PredictionStatus = PredictionStatus.COMPLETED  # a sync answer is always completed
    model: PredictModelRef
    output: Any = None
    usage: PredictUsage
    metadata: dict[str, Any]
    error: PredictionError | None = None
    expired: bool = False  # the retention purge dropped input and output
    created_at: datetime


class PredictOutcome(BaseModel):
    """What the predict service hands the route: the envelope, the status it travels with and its extra headers."""

    envelope: PredictionEnvelope
    status_code: int = HTTPStatus.OK
    headers: dict[str, str] = Field(default_factory=dict)


class DeploymentSnippets(BaseModel):
    curl: str
    javascript: str
    python: str


class DeploymentSchema(BaseModel):
    """What to send to and expect from POST /v1/predict for this deployment, derived from its task type, with call snippets."""

    task_type: str
    model_id: uuid.UUID
    model_revision: str
    input: dict[str, Any]
    output: dict[str, Any]
    snippets: DeploymentSnippets
