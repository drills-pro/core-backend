"""The train job's spec, built for a run that has an id but no row yet. The job gets presigned reads of the dataset
version and the run's staging prefix, nothing else (no lakeFS or MLflow credential, docs/trainer-plan.md T7)."""
import uuid
from http import HTTPStatus
from typing import Any

from app.core.bundle_manifest import hyperparameter_errors
from app.core.presets import HyperparameterRange, TaskPreset
from app.core.problem import Problem
from app.core.requirements import requirements_sha256
from app.core.settings import settings
from app.core.task_types import TaskInfo
from app.integrations import data_plane
from app.integrations.data_plane import train_prefix
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


def bundle_hyperparameters(preset: TaskPreset, asked: HyperparameterOverrides, manifest: dict) -> dict[str, Any]:
    """A run on a task bundle: the preset's default values where the request is silent, checked against the bundle's JSON
    Schema (a 422 naming every problem)."""
    defaults = preset.hyperparameters if isinstance(preset.hyperparameters, dict) else {}
    values = {**defaults, **asked.model_dump(exclude_none=True)}
    if errors := hyperparameter_errors(manifest["hyperparameters"], values):
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.INVALID_HYPERPARAMETERS, "; ".join(errors))
    return values


async def build(
    tenant_id: str, project_id: uuid.UUID, run_id: uuid.UUID, dataset_id: uuid.UUID, task: TaskInfo, body: TrainingRunCreate,
    preset: TaskPreset, hyperparameters: Hyperparameters | dict[str, Any], reporting: Reporting | None = None, pins: tuple[str, str] | None = None,
    requirements: list[str] | None = None,
) -> TrainSpec:
    """`pins` is (core sha256, bundle sha256) for a task trained by a bundle; the spec records both, and the hash of the
    merged `requirements` the run installs."""
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
            prefix=train_prefix(*ids, str(run_id)),
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
            core_sha256=pins and pins[0],
            bundle_sha256=pins and pins[1],
            requirements_sha256=requirements_sha256(requirements) if requirements else None,
        ),
        resources=Resources(cpu=preset.cpu, memory_bytes=preset.memory_bytes, gpu=preset.gpu, vram_gb=preset.vram_gb, max_runtime_s=preset.max_runtime_s),
    )
