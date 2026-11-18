"""Response shapes for the model-registry routes."""
import uuid
from datetime import datetime

from pydantic import BaseModel

from app.integrations.data_plane import ModelStage
from app.modules.user.model_registry.constants import PromotionStatus


class VersionRef(BaseModel):
    """A model_versions row as stored: `mlflow_model_name` is MLflow's tenant- and project-namespaced name."""

    id: uuid.UUID
    mlflow_model_name: str
    mlflow_version: str
    stage: ModelStage


class ModelVersion(VersionRef):
    created_at: datetime


class Promotion(BaseModel):
    """A promotion to Production that has not finished: pending (the stage flips once the deployment is active) or failed (stages unchanged)."""

    promotion_status: PromotionStatus | None = None
    promotion_error: str | None = None
    promotion_error_code: str | None = None  # the problem code of the error, e.g. deploy_risk_unacknowledged


class ModelVersionItem(ModelVersion, Promotion):
    customer_label: str
    model_id: uuid.UUID  # X-Model-Id of POST /v1/predict; the revision there is mlflow_version
    training_run_id: uuid.UUID  # the run the version was registered from, for its scores and lineage
    publish_metadata: bool
    publish_artifact: bool


class PromotedVersion(VersionRef, Promotion):
    customer_label: str


class PublishState(BaseModel):
    """Per-version "exclude" switches: true (the default) lets the project's Sharing level decide, false keeps this one out."""

    id: uuid.UUID
    publish_metadata: bool
    publish_artifact: bool


class RegisteredVersion(BaseModel):
    """A version MLflow has created, not yet recorded in model_versions."""

    mlflow_model_name: str
    mlflow_version: str
