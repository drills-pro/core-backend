"""The job spec: the one document core-api hands every Ray job (planning 06 "Job spec v1", generalised).

An envelope every job shares (who, what it may use) plus a `payload` that depends on `kind`: `ingest` or `train`. The job reads this document and nothing
else; secrets are never in it (a train spec carries presigned reads, which expire). It is stored verbatim for audit and replay, and its JSON Schema is committed as
`contracts/job-spec.v1.json` (tests/integrations/test_job_spec.py keeps the two equal).

Adding a kind = a payload model, a spec class, and a member of `AnyJobSpec` (discriminator `kind`). Changing a field
that a running job reads = a new `spec_version`; the job refuses versions it does not know.
"""
import uuid
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter, model_validator

from app.core.enums import ImportSource, Split, TaskType
from app.core.settings import DatasetLimits
from app.core.task_catalog import DataKind, Preprocessing, TextPreprocessing, TrainBaseModel
from app.core.task_types import TRAINER_PATTERN

SPEC_VERSION = 1


class JobKind(StrEnum):
    INGEST = "ingest"
    TRAIN = "train"


class Resources(BaseModel):
    """Declared, never inferred (planning 06): the scheduler and quotas act on these."""

    cpu: float = Field(gt=0)
    memory_bytes: int = Field(gt=0)
    gpu: float = Field(default=0, ge=0)
    max_runtime_s: int = Field(default=0, ge=0)  # 0 = unlimited


# ---- ingest ----

class IngestBase(BaseModel):
    """Presigned reads of the base version a merge import builds on: index (images) or data + schema (tabular)."""

    index_url: str | None = None
    data_url: str | None = None
    schema_url: str | None = None


class IngestHf(BaseModel):
    repo: str
    revision: str
    column: str | None = None


class IngestPayload(BaseModel):
    """What the ingest entrypoints read (ray_jobs.common.enums.Param mirrors these keys; contract-tested)."""

    bucket: str  # staging bucket; the job writes only under `prefix`
    prefix: str
    source: ImportSource
    task: TaskType
    target: str | None = None
    split: Split | None = None
    limits: DatasetLimits
    base: IngestBase | None = None
    hf: IngestHf | None = None


# ---- train ----

DEFAULT_EPOCHS, MAX_EPOCHS = 30, 500
DEFAULT_BATCH_SIZE, MAX_BATCH_SIZE = 16, 512
DEFAULT_IMAGE_SIZE, MIN_IMAGE_SIZE, MAX_IMAGE_SIZE = 224, 32, 1536
DEFAULT_SEED = 42


class Hyperparameters(BaseModel):
    """What a caller may tune; every field has the platform default, so a request names only what it changes."""

    epochs: int = Field(default=DEFAULT_EPOCHS, ge=1, le=MAX_EPOCHS)
    batch_size: int = Field(default=DEFAULT_BATCH_SIZE, ge=1, le=MAX_BATCH_SIZE)
    image_size: int = Field(default=DEFAULT_IMAGE_SIZE, ge=MIN_IMAGE_SIZE, le=MAX_IMAGE_SIZE)


class TrainData(BaseModel):
    """Presigned reads of one dataset version, valid past `resources.max_runtime_s`. The trainer holds no lakeFS credential."""

    index_url: str | None = None  # images: index.parquet (columns layout.INDEX_COLUMNS)
    manifest_url: str | None = None  # images: JSON object {index path: presigned GET of that image}, staged under `prefix`
    data_url: str | None = None  # text: data.parquet, a table with the catalog's TEXT_COLUMN and LABEL_COLUMN
    version_id: uuid.UUID
    lakefs_commit: str


class TrainPayload(BaseModel):
    """What the train driver reads. It writes `model.onnx`, `model.json` and `report.json` under `prefix` in `bucket`."""

    bucket: str  # staging bucket; the job writes only under `prefix`
    prefix: str
    task: TaskType
    trainer: str = Field(pattern=TRAINER_PATTERN)  # "module:function" in the ray_training package: the task_types row's trainer
    data_kind: DataKind  # which urls `data` carries
    preset: str  # the task_presets row's name; the values it stood for are in the fields below
    base_model: TrainBaseModel
    learning_rate: float = Field(gt=0)  # the catalog's value for this (task, preset): the trainer keeps no table of its own
    preprocessing: Preprocessing | TextPreprocessing  # likewise; the trainer applies it and records it in model.json
    data: TrainData
    hyperparameters: Hyperparameters
    seed: int = DEFAULT_SEED

    @model_validator(mode="after")
    def _data_fits_the_task(self):
        """Image tasks read an index and a manifest, table tasks a data file; the trainer reads only what its task needs."""
        have = (self.data.data_url,) if self.data_kind is not DataKind.IMAGES else (self.data.index_url, self.data.manifest_url)
        if not all(have):
            raise ValueError(f"the data of a {self.task} run is missing a url")
        return self


# ---- the document ----

class _Envelope(BaseModel):
    spec_version: Literal[1] = SPEC_VERSION
    job_id: str  # the submission id: how core-api stops the job even when the submit answer is lost
    tenant_id: uuid.UUID
    resources: Resources


class IngestSpec(_Envelope):
    kind: Literal[JobKind.INGEST] = JobKind.INGEST
    payload: IngestPayload


class TrainSpec(_Envelope):
    kind: Literal[JobKind.TRAIN] = JobKind.TRAIN
    payload: TrainPayload  # resources.gpu = Ray Train workers, one GPU each (a fraction: one worker on that share); the driver holds none


AnyJobSpec = Annotated[IngestSpec | TrainSpec, Field(discriminator="kind")]
SPEC_ADAPTER: TypeAdapter[IngestSpec | TrainSpec] = TypeAdapter(AnyJobSpec)  # its JSON Schema is contracts/job-spec.v1.json
