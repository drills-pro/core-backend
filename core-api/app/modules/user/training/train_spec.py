"""The train job's spec, built for a run that has an id but no row yet. The job gets presigned reads of the dataset
version and the run's staging prefix, nothing else (no lakeFS or MLflow credential, docs/trainer-plan.md T7)."""
import uuid

from app.core.enums import TaskType
from app.core.settings import settings
from app.integrations import data_plane
from app.integrations.data_plane import layout
from app.integrations.job_spec import Resources, TrainPayload, TrainSpec
from app.modules.user.training.constants import (
    BASE_MODELS,
    JOB_ID_PREFIX,
    LOST_JOB_GRACE_SECONDS,
    MAX_PRESIGN_SECONDS,
    TRAIN_GPUS,
)
from app.modules.user.training.schemas import TrainingRunCreate


def submission_id(run_id: uuid.UUID) -> str:
    return f"{JOB_ID_PREFIX}{run_id}"


def horizon_seconds() -> int:
    """How long after submission a run may still be using its presigned reads: its place in the queue (one GPU, so
    runs go one at a time, and at most `max_concurrent_training_runs` wait) plus its own runtime, plus some grace."""
    waiting_and_running = settings.ray.max_concurrent_training_runs * settings.training.max_runtime_seconds
    return waiting_and_running + LOST_JOB_GRACE_SECONDS


async def build(
    tenant_id: str, project_id: uuid.UUID, run_id: uuid.UUID, dataset_id: uuid.UUID, task: TaskType, body: TrainingRunCreate
) -> TrainSpec:
    ids = (tenant_id, str(project_id))
    data = await data_plane.prepare_train_data(
        *ids, str(dataset_id), str(body.dataset_version_id), str(run_id), min(horizon_seconds(), MAX_PRESIGN_SECONDS)
    )
    cfg = settings.training
    return TrainSpec(
        job_id=submission_id(run_id),
        tenant_id=tenant_id,
        payload=TrainPayload(
            bucket=settings.data_plane.staging_bucket,
            prefix=layout.train_prefix(*ids, str(run_id)),
            task=task,
            preset=body.preset,
            base_model=BASE_MODELS[(task, body.preset)],
            data=data,
            hyperparameters=body.hyperparameters,
            seed=body.seed,
        ),
        resources=Resources(cpu=cfg.task_cpus, memory_bytes=cfg.task_memory_bytes, gpu=TRAIN_GPUS, max_runtime_s=cfg.max_runtime_seconds),
    )
