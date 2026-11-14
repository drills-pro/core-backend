import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import CatalogSource, CatalogStatus
from app.modules.admin.model_catalog.constants import DEFAULT_SOURCE, MAX_NAME_LENGTH, SHA_PATTERN


class CatalogModelCreate(BaseModel):
    """A model to admit. `license` is replaced by the one the Hub declares when the Hub is configured."""

    model_config = ConfigDict(extra="forbid")

    source: CatalogSource = DEFAULT_SOURCE
    repo_id: str = Field(min_length=1)
    revision: str = Field(pattern=SHA_PATTERN)
    display_name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    task: str = Field(min_length=1)  # a task_types slug
    license: str = Field(min_length=1)
    params: int | None = Field(default=None, gt=0)
    size_bytes: int | None = Field(default=None, ge=0)
    status: CatalogStatus = CatalogStatus.ENABLED


class CatalogModelUpdate(BaseModel):
    """Metadata an admin may change; a different repo or revision is a different model, so it is a new entry."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)
    task: str | None = Field(default=None, min_length=1)
    license: str | None = Field(default=None, min_length=1)
    params: int | None = Field(default=None, gt=0)
    size_bytes: int | None = Field(default=None, ge=0)


class AdminCatalogModel(BaseModel):
    id: uuid.UUID
    source: CatalogSource
    repo_id: str
    revision: str
    display_name: str
    task: str
    license: str
    params: int | None
    size_bytes: int | None
    status: CatalogStatus
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime
