"""Rows of the list_public_*() functions; each mirrors its RETURNS TABLE (tests/architecture/test_admin_shapes.py)."""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class PublicProject(BaseModel):
    id: uuid.UUID
    name: str | None  # null unless the project publishes its metadata
    task_type: str | None
    created_at: datetime


class PublicDataset(BaseModel):
    """A private dataset of a public project is listed with no name: "exists" is visible, contents are not."""

    id: uuid.UUID
    name: str | None
    published: bool
    version_number: int | None
    stats: Any


class PublicModel(BaseModel):
    """Likewise: an unpublished model's name and version are null."""

    id: uuid.UUID
    mlflow_model_name: str | None
    mlflow_version: str | None
    publish_metadata: bool
    publish_artifact: bool
