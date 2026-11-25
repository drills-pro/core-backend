"""Response shapes for the model-registry routes."""
import uuid
from datetime import datetime

from pydantic import BaseModel


class VersionRef(BaseModel):
    """A model_versions row as stored: `mlflow_model_name` is MLflow's tenant- and project-namespaced name."""

    id: uuid.UUID
    mlflow_model_name: str
    mlflow_version: str


class ModelVersion(VersionRef):
    created_at: datetime


class ModelVersionItem(ModelVersion):
    model_id: uuid.UUID  # the project's registered model the version belongs to


class RegisteredVersion(BaseModel):
    """A version MLflow has created, not yet recorded in model_versions."""

    mlflow_model_name: str
    mlflow_version: str
