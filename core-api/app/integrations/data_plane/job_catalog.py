"""Ray ingest job types: each JobType names a module in ray-jobs/ray_jobs/entrypoints/ that runs it.

Adding one = the module and a JobType member.
"""
from enum import StrEnum

from app.core import task_catalog
from app.core.enums import ImportSource, TaskType


class JobType(StrEnum):
    """Names of the catalog entries; tests/ingest_job/test_contract.py checks each one is an importable module."""

    UPLOAD_IMAGES = "upload_images"
    UPLOAD_CSV = "upload_csv"
    HUGGINGFACE = "huggingface"


ENTRYPOINTS = "ray_jobs.entrypoints"  # one module per JobType, run as `python -m`
TRAIN_ENTRYPOINT = "ray_training.entrypoints.train"  # the train driver (top-level ray-training/); not an ingest JobType


def ingest_job_type(source: str, task_type: str) -> JobType:
    """Which catalog entry imports this source for this task."""
    if source == ImportSource.HUGGINGFACE:
        return JobType.HUGGINGFACE
    return JobType.UPLOAD_CSV if task_catalog.is_table(TaskType(task_type)) else JobType.UPLOAD_IMAGES
