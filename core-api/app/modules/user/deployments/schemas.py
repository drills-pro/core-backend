"""Response shapes for the deployment routes."""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


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
