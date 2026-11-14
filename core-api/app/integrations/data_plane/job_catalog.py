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
TRAIN_ENTRYPOINT = "ray_core.entrypoints.train"  # the train driver in the ray_training package (ray-training/ray_core); not an ingest JobType
# A run on a task_types row from before bundles: the driver of a ray_training zip from before the ray_core split, which still
# holds the trainers those rows name. Such a run works only while that zip is the stable one (planning/25 migration path).
LEGACY_TRAIN_ENTRYPOINT = "ray_training.entrypoints.train"
SMOKE_ENTRYPOINTS = {  # one per package: a tiny run through the package's own entrypoints, run before it can be promoted
    JobPackageName.RAY_JOBS: "ray_jobs.entrypoints.smoke",
    JobPackageName.RAY_TRAINING: "ray_core.entrypoints.smoke_all",  # runs the bundles' smokes: every stable one for a core, the one for a bundle
    JobPackageName.RAY_SERVING: "ray_serving.entrypoints.smoke",
}
