import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.authz import Scope


class NewApiKey(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[Scope] = Field(min_length=1)
    model_ids: list[uuid.UUID] = []  # empty = every model in the project
    expires_at: datetime | None = None


class ApiKey(BaseModel):
    """A key as listed: never its secret or hash."""

    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    model_ids: list[uuid.UUID]
    expires_at: datetime | None
    created_at: datetime


class ListedApiKey(ApiKey):
    created_by: str
    revoked_at: datetime | None


class CreatedApiKey(ApiKey):
    secret: str  # in the create response only
