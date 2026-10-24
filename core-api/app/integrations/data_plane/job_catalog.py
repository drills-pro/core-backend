"""The catalog of Ray job types: one entry per job type, naming the module that runs it.

Adding an ingest job type = a module in ray-jobs/ray_jobs/entrypoints/, a JobType member and one entry here.
Image flavour and resources join JobSpec when job submission moves to RayJob; training types join as kind=TRAINING.
"""
from enum import StrEnum

from pydantic import BaseModel

from app.core.enums import TABULAR_TASKS, ImportSource, TaskType


class JobKind(StrEnum):
    INGEST = "ingest"


class JobType(StrEnum):
    """Names of the catalog entries; tests/ingest_job/test_contract.py checks each one is an importable module."""

    UPLOAD_IMAGES = "upload_images"
    UPLOAD_CSV = "upload_csv"
    HUGGINGFACE = "huggingface"


class JobSpec(BaseModel):
    kind: JobKind
    entrypoint: str  # module run as `python -m`


_ENTRYPOINTS = "ray_jobs.entrypoints"
CATALOG: dict[JobType, JobSpec] = {
    t: JobSpec(kind=JobKind.INGEST, entrypoint=f"{_ENTRYPOINTS}.{t.value}") for t in JobType
}


def ingest_job_type(source: str, task_type: str) -> JobType:
    """Which catalog entry imports this source for this task."""
    if source == ImportSource.HUGGINGFACE:
        return JobType.HUGGINGFACE
    return JobType.UPLOAD_CSV if TaskType(task_type) in TABULAR_TASKS else JobType.UPLOAD_IMAGES
