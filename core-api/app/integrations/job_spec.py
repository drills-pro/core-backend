"""The job spec: the one document core-api hands every Ray job (planning 06 "Job spec v1", generalised).

An envelope every job shares (who, what it may use) plus a `payload` that depends on `kind`: today only `ingest`. The job reads this document and nothing
else; secrets are never in it. It is stored verbatim for audit and replay, and its JSON Schema is committed as
`contracts/job-spec.v1.json` (tests/integrations/test_job_spec.py keeps the two equal).

Adding a kind = a payload model, a spec class, and a union with `IngestSpec` (discriminator `kind`). Changing a field
that a running job reads = a new `spec_version`; the job refuses versions it does not know.
"""
import uuid
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from app.core.enums import ImportSource, TaskType
from app.core.settings import DatasetLimits
from app.integrations.data_plane.layout import Split

SPEC_VERSION = 1


class JobKind(StrEnum):
    INGEST = "ingest"


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


# ---- the document ----

class _Envelope(BaseModel):
    spec_version: Literal[1] = SPEC_VERSION
    job_id: str  # the submission id: how core-api stops the job even when the submit answer is lost
    tenant_id: uuid.UUID
    resources: Resources


class IngestSpec(_Envelope):
    kind: Literal[JobKind.INGEST] = JobKind.INGEST
    payload: IngestPayload
