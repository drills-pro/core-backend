"""The train job's spec, built for a run that has an id but no row yet. The job gets presigned reads of the dataset
version and the run's staging prefix, nothing else (no lakeFS or MLflow credential, docs/trainer-plan.md T7)."""
import uuid
from http import HTTPStatus

from app.core.presets import HyperparameterRange, TaskPreset
from app.core.problem import Problem
from app.core.settings import settings
from app.core.task_types import TaskInfo
from app.integrations import data_plane
from app.integrations.data_plane import layout
from app.integrations.job_spec import Hyperparameters, Reporting, Resources, TrainPayload, TrainSpec
from app.modules.user.training.constants import (
    JOB_ID_PREFIX,
    LOST_JOB_GRACE_SECONDS,
    MAX_PRESIGN_SECONDS,
    ProblemCode,
)
from app.modules.user.training.schemas import HyperparameterOverrides, TrainingRunCreate


def submission_id(run_id: uuid.UUID) -> str:
    return f"{JOB_ID_PREFIX}{run_id}"


def horizon_seconds() -> int:
    """How long after submission a run may still be using its presigned reads: its place in the queue (one GPU, so
    runs go one at a time, and at most `max_concurrent_training_runs` wait) plus its own runtime, plus some grace."""
    waiting_and_running = settings.ray.max_concurrent_training_runs * settings.training.max_runtime_seconds
    return waiting_and_running + LOST_JOB_GRACE_SECONDS


def _pick(name: str, bounds: HyperparameterRange, asked: int | None) -> int:
    if asked is None:
        return bounds.default
    if not bounds.min <= asked <= bounds.max:
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.HYPERPARAMETER_OUT_OF_RANGE, f"{name} must be between {bounds.min} and {bounds.max}")
    return asked


def resolve_hyperparameters(preset: TaskPreset, asked: HyperparameterOverrides) -> Hyperparameters:
    """The preset's defaults where the request is silent, its bounds where it is not."""
    limits = preset.hyperparameters
    return Hyperparameters(
        epochs=_pick("epochs", limits.epochs, asked.epochs),
        batch_size=_pick("batch_size", limits.batch_size, asked.batch_size),
        image_size=_pick("image_size", limits.image_size, asked.image_size),
    )


async def build(
    tenant_id: str, project_id: uuid.UUID, run_id: uuid.UUID, dataset_id: uuid.UUID, task: TaskInfo, body: TrainingRunCreate,
    preset: TaskPreset, hyperparameters: Hyperparameters, reporting: Reporting | None = None,
) -> TrainSpec:
    ids = (tenant_id, str(project_id))
    data = await data_plane.prepare_train_data(
        *ids, str(dataset_id), str(body.dataset_version_id), str(run_id), min(horizon_seconds(), MAX_PRESIGN_SECONDS),
        data_kind=task.data_kind,
    )
    return TrainSpec(
        job_id=submission_id(run_id),
        tenant_id=tenant_id,
        reporting=reporting,
        payload=TrainPayload(
            bucket=settings.data_plane.staging_bucket,
            prefix=layout.train_prefix(*ids, str(run_id)),
            task=task.slug,
            trainer=task.trainer,
            data_kind=task.data_kind,
            preset=preset.name,
            base_model=preset.base_model,
            learning_rate=preset.learning_rate,
            preprocessing=preset.preprocessing,
            lr_schedule=preset.lr_schedule,
            warmup_fraction=preset.warmup_fraction,
            augmentation=preset.augmentation,
            early_stopping_patience=preset.early_stopping_patience,
            data=data,
            hyperparameters=hyperparameters,
            seed=body.seed,
        ),
        resources=Resources(cpu=preset.cpu, memory_bytes=preset.memory_bytes, gpu=preset.gpu, max_runtime_s=preset.max_runtime_s),
    )
