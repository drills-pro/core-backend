"""Rows of admin_models(), admin_model_versions() and admin_model_stats(); each mirrors its function's RETURNS TABLE
(tests/architecture/test_admin_shapes.py)."""
import uuid
from datetime import datetime

from pydantic import BaseModel

from app.modules.admin.models.constants import ModelStatus


class AdminModelVersion(BaseModel):
    id: uuid.UUID
    version: str
    stage: str
    status: ModelStatus
    created_at: datetime


class AdminModel(AdminModelVersion):
    """One model: its newest version, plus what it belongs to."""

    tenant_id: uuid.UUID
    tenant_name: str
    project_id: uuid.UUID
    project_name: str
    task_type: str | None
    visibility: str
    model_name: str
    version_count: int


class ModelStats(BaseModel):
    model_count: int
    deployed_count: int
    deploying_count: int
    stopped_count: int
    not_deployed_count: int
    project_count: int
    version_count: int
