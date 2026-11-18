import uuid
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field, computed_field

from app.core.enums import TaskType
from app.core.enums import Split
from app.integrations.data_plane import ImportReport
from app.modules.user.datasets.constants import (
    DATASET_RETENTION_DAYS,
    HF_REPO_PATTERN,
    HF_REVISION_PATTERN,
    PARAM_MAX_LENGTH,
    DataCategory,
    ImportSource,
    ImportStatus,
)


class DatasetCreate(BaseModel):
    name: str = Field(min_length=1)
    data_categories: list[DataCategory] = Field(default_factory=list, description="what the dataset holds, declared by the customer")


class DatasetSummary(BaseModel):
    """`task_type` is the project's: one task type per project, never stored on the dataset. None for a project
    without one, which cannot import."""

    id: uuid.UUID
    name: str
    task_type: TaskType | None
    created_at: datetime
    deleted_at: datetime | None
    published_version_id: uuid.UUID | None
    data_categories: list[DataCategory]

    @computed_field
    @property
    def restorable_until(self) -> datetime | None:
        """When a deleted dataset is purged for good; None while it is live."""
        return None if self.deleted_at is None else self.deleted_at + timedelta(days=DATASET_RETENTION_DAYS)


class TrashedDataset(BaseModel):
    """A deleted dataset in the Trash, with the project it came from."""

    id: uuid.UUID
    name: str
    project_id: uuid.UUID
    project_name: str
    deleted_at: datetime

    @computed_field
    @property
    def restorable_until(self) -> datetime:
        return self.deleted_at + timedelta(days=DATASET_RETENTION_DAYS)


class PublishRequest(BaseModel):
    version_id: uuid.UUID


class DatasetVersion(BaseModel):
    id: uuid.UUID
    number: int
    import_id: uuid.UUID
    stats: dict[str, Any]
    created_at: datetime


class ImportParams(BaseModel):
    """What the ingest job needs besides the upload. Stored as dataset_imports.params."""

    target: str | None = Field(default=None, min_length=1, max_length=PARAM_MAX_LENGTH)  # tabular upload: the target column (required)
    split: Split | None = None  # tag every row with this split
    repo: str | None = Field(default=None, max_length=PARAM_MAX_LENGTH, pattern=HF_REPO_PATTERN)  # huggingface: public repo id
    # huggingface: branch or sha, default main. The job pins the commit sha the Hub answers with and downloads at that sha.
    revision: str | None = Field(default=None, max_length=PARAM_MAX_LENGTH, pattern=HF_REVISION_PATTERN)
    column: str | None = Field(default=None, min_length=1, max_length=PARAM_MAX_LENGTH)  # huggingface: which valid candidate column to use


class ImportCreate(BaseModel):
    source: ImportSource
    task_type: TaskType | None = None  # optional echo of the project's; a different value is refused
    base_version_id: uuid.UUID | None = None
    params: ImportParams = ImportParams()


class DatasetImport(BaseModel):
    id: uuid.UUID
    dataset_id: uuid.UUID
    source: ImportSource
    status: ImportStatus
    base_version_id: uuid.UUID | None
    params: ImportParams
    report: ImportReport | None
    expires_at: datetime | None
    created_at: datetime
    completed_at: datetime | None
    upload_url: str | None = None  # only on create, for source=upload


class PreviewRow(BaseModel):
    """One index.parquet or data.parquet row. `url` is a presigned GET of the image, when there is one."""

    data: dict[str, Any]
    url: str | None = None


class DatasetPreview(BaseModel):
    rows: list[PreviewRow]


class FileLink(BaseModel):
    url: str
