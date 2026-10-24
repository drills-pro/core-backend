"""The job spec: the one document core-api hands every Ray job (planning 06 "Job spec v1", generalised).

An envelope every job shares (who, which attempt, what it may use) plus a `payload` that depends on `kind`:
`ingest` (dataset import and, later, dataset preparation) and `train`. The job reads this document and nothing
else; secrets are never in it. It is stored verbatim for audit and replay, and its JSON Schema is committed as
`contracts/job-spec.v1.json` (tests/integrations/test_job_spec.py keeps the two equal).

Adding a kind = a payload model, a `JobSpec` union member, and an entry in the job catalog. Changing a field
that a running job reads = a new `spec_version`; the job refuses versions it does not know.
"""
import uuid
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

from app.core.enums import ImportSource, TaskType
from app.core.settings import DatasetLimits
from app.integrations.data_plane.layout import Split

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


class Reporting(BaseModel):
    """Where a job that streams events reports to; the token is injected as an env var, never put here."""

    events_url: str
    token_env: str


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

class ModelSource(StrEnum):
    HF = "hf"


class DataFormat(StrEnum):
    WEBDATASET = "webdataset"


class TrainTask(BaseModel):
    type: TaskType
    preset: str


class BaseModelRef(BaseModel):
    """Pinned by catalog id and commit, never fetched from the Hub at run time."""

    catalog_id: uuid.UUID
    source: ModelSource
    repo_id: str
    revision: str
    license: str
    uri: str


class TrainData(BaseModel):
    """Pinned by lakeFS commit, so retraining on the same data is exact."""

    repo: str
    commit: str
    path: str
    format: DataFormat
    manifest_url: str
    shuffle_seed: int
    test_commit: str | None = None


class TrainTracking(BaseModel):
    mlflow_experiment: str
    tags: dict[str, str] = Field(default_factory=dict)


class TrainOutput(BaseModel):
    artifact_prefix: str
    register_as: str | None = None


class TrainPayload(BaseModel):
    task: TrainTask
    base_model: BaseModelRef
    data: TrainData
    hyperparameters: dict[str, Any] = Field(default_factory=dict)  # validated per preset by the trainer
    tracking: TrainTracking
    output: TrainOutput


# ---- the document ----

class _Envelope(BaseModel):
    spec_version: Literal[1] = SPEC_VERSION
    job_id: str  # the submission id: how core-api stops the job even when the submit answer is lost
    attempt: int = Field(default=1, ge=1)  # increments on every resubmit; fences a stale run's writes
    tenant_id: uuid.UUID
    resources: Resources
    reporting: Reporting | None = None


class IngestSpec(_Envelope):
    kind: Literal[JobKind.INGEST] = JobKind.INGEST
    payload: IngestPayload


class TrainSpec(_Envelope):
    kind: Literal[JobKind.TRAIN] = JobKind.TRAIN
    payload: TrainPayload


JobSpec = Annotated[IngestSpec | TrainSpec, Field(discriminator="kind")]
JOB_SPEC = TypeAdapter(JobSpec)
