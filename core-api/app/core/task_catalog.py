"""What the platform knows about each supported task type, in one table: a new task type is one entry here (plus a
member of `TaskType` and, for a task not in projects.task_type's CHECK yet, a migration).

`TASKS` carries what differs between task types: the kind of data a task works on, whether the trainer can train it, and
whether a deployment can serve its models. What a trainable task trains with (checkpoint pinned to a commit, learning
rate, preprocessing, resources, hyperparameter bounds) is rows of `task_presets` (app/core/presets.py), changed through
/v1/admin without a deploy; the train job spec carries the values, the trainer holds no table. Everything that asks "is
it supported / tabular / trainable / servable" asks this module; `job_spec.TrainTask` and the deployment error text derive
from it. Report parsing, model registration and metering do not branch on the task.

A model that normalises differently (CLIP, 0.5/0.5 ViTs) sets its own `Preprocessing` in its preset row. A text task's
preprocessing is `TextPreprocessing`.
"""
from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import TaskType
from app.core.problem import Problem


class DataKind(StrEnum):
    IMAGES = "images"
    TABULAR = "tabular"
    TEXT = "text"  # a table (data.parquet) with TEXT_COLUMN and LABEL_COLUMN


TEXT_COLUMN = "text"  # the columns a text dataset must have (ray_jobs.common.constants mirrors these)
LABEL_COLUMN = "label"


class TrainBaseModel(BaseModel):
    """A pretrained checkpoint on the Hugging Face Hub, pinned to a commit."""

    repo_id: str
    revision: str


class Resample(StrEnum):
    """PIL resampling filter of the resize to a square `image_size` (the trainer mirrors this enum, contract-tested)."""

    BILINEAR = "bilinear"
    BICUBIC = "bicubic"


IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)
DEFAULT_RESCALE = 1 / 255  # 8-bit pixel to [0, 1]


class Preprocessing(BaseModel):
    """How an image becomes the model's input: resize (stretch, to a square `image_size`) with `resample`, multiply by
    `rescale`, then (x - mean) / std per RGB channel. Trained with, recorded in model.json, applied again when serving."""

    model_config = ConfigDict(frozen=True)

    mean: tuple[float, float, float]
    std: tuple[float, float, float]
    rescale: float = Field(gt=0)
    resample: Resample


IMAGENET_PREPROCESSING = Preprocessing(mean=IMAGENET_MEAN, std=IMAGENET_STD, rescale=DEFAULT_RESCALE, resample=Resample.BICUBIC)


DEFAULT_MAX_LENGTH = 256


class TextPreprocessing(BaseModel):
    """How text becomes the model's input: the base model's tokenizer, truncated and padded to `max_length` tokens.
    Trained with, recorded in model.json (with tokenizer.json), applied again when serving."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_length: int = Field(gt=0)


DEFAULT_TEXT_PREPROCESSING = TextPreprocessing(max_length=DEFAULT_MAX_LENGTH)


class TaskInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    data_kind: DataKind
    trainable: bool = False  # the trainer has a trainer for it; what it trains with is the task_presets rows (app/core/presets.py)
    servable: bool = False  # the serving script in serve_client can serve its models


TASKS: Mapping[TaskType, TaskInfo] = {
    TaskType.IMAGE_CLASSIFICATION: TaskInfo(data_kind=DataKind.IMAGES, trainable=True, servable=True),
    TaskType.OBJECT_DETECTION: TaskInfo(data_kind=DataKind.IMAGES, trainable=True, servable=True),
    TaskType.TEXT_CLASSIFICATION: TaskInfo(data_kind=DataKind.TEXT, trainable=True, servable=True),
    TaskType.TABULAR_CLASSIFICATION: TaskInfo(data_kind=DataKind.TABULAR),
    TaskType.TABULAR_REGRESSION: TaskInfo(data_kind=DataKind.TABULAR),
}


TRAINABLE: tuple[TaskType, ...] = tuple(t for t, i in TASKS.items() if i.trainable)  # job_spec.TrainTask is these
SERVABLE: tuple[TaskType, ...] = tuple(t for t, i in TASKS.items() if i.servable)


def info(task: TaskType) -> TaskInfo:
    return TASKS[task]


def supported(task: TaskType) -> bool:
    return task in TASKS


def is_tabular(task: TaskType) -> bool:
    return task in TASKS and TASKS[task].data_kind is DataKind.TABULAR


def is_table(task: TaskType) -> bool:
    """The dataset is a table (data.parquet + schema.json) rather than an image index."""
    return task in TASKS and TASKS[task].data_kind in (DataKind.TABULAR, DataKind.TEXT)


def trainable(task: TaskType) -> bool:
    return task in TASKS and TASKS[task].trainable


def servable(task: TaskType) -> bool:
    return task in TASKS and TASKS[task].servable


def require_supported_task_type(task_type: TaskType) -> None:
    if not supported(task_type):
        raise Problem(422, "task_type_not_supported", f"task type '{task_type}' is not supported yet")
