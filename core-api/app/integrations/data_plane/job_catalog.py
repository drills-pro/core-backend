"""Ray ingest job types: each JobType names a module in ray-jobs/ray_jobs/entrypoints/ that runs it.

Adding one = the module and a JobType member.
"""
from enum import StrEnum

from app.core.enums import JobPackageName


class JobType(StrEnum):
    """Names of the catalog entries; tests/ingest_job/test_contract.py checks each one is an importable module."""

    UPLOAD_IMAGES = "upload_images"
    UPLOAD_CSV = "upload_csv"
    HUGGINGFACE = "huggingface"


ENTRYPOINTS = "ray_jobs.entrypoints"  # one module per JobType, run as `python -m`
TRAIN_ENTRYPOINT = "ray_training.entrypoints.train"  # the train driver (top-level ray-training/); not an ingest JobType
SMOKE_ENTRYPOINTS = {  # one per package: a tiny run through the package's own entrypoints, run before it can be promoted
    JobPackageName.RAY_JOBS: "ray_jobs.entrypoints.smoke",
    JobPackageName.RAY_TRAINING: "ray_training.entrypoints.smoke",
}
