"""Response shapes for the model-registry routes."""
import uuid
from datetime import datetime

from pydantic import BaseModel

from app.integrations.data_plane import ModelStage


class VersionRef(BaseModel):
    """A model_versions row as stored: `mlflow_model_name` is MLflow's tenant- and project-namespaced name."""

    id: uuid.UUID
    mlflow_model_name: str
    mlflow_version: str
    stage: ModelStage


class ModelVersion(VersionRef):
    created_at: datetime


class ModelVersionItem(ModelVersion):
    customer_label: str
    model_id: uuid.UUID  # X-Model-Id of POST /v1/predict; the revision there is mlflow_version


class PromotedVersion(VersionRef):
    customer_label: str


class PublishState(BaseModel):
    id: uuid.UUID
    publish_metadata: bool
    publish_artifact: bool


class RegisteredVersion(BaseModel):
    """A version MLflow has created, not yet recorded in model_versions."""

    mlflow_model_name: str
    mlflow_version: str
