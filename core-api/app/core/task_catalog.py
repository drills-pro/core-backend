"""What the platform knows about each supported task type, in one table: a new task type is one entry here (plus a
member of `TaskType` and, for a task not in projects.task_type's CHECK yet, a migration).

`TASKS` carries what differs between task types: the kind of data a task works on, per preset for a task the trainer
can run its pretrained checkpoint (pinned to a commit), learning rate and image preprocessing (docs/trainer-plan.md
T5/T6), and whether a deployment can serve its models. The trainer holds no table of its own: the train job spec
carries these values. Everything that asks "is it supported / tabular / trainable / servable" asks this module;
`job_spec.TrainTask` and the deployment error text derive from it. Report parsing, model registration and metering do
not branch on the task.

A new Hugging Face model for an existing task is one `PresetSpec` here; a model that normalises differently (CLIP,
0.5/0.5 ViTs) sets its own `Preprocessing`. A text task's preprocessing is `TextPreprocessing`.
"""
from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import TaskType, TrainPreset
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


class PresetSpec(BaseModel):
    """What a preset of a trainable task trains with."""

    model_config = ConfigDict(frozen=True)

    base_model: TrainBaseModel
    learning_rate: float = Field(gt=0)
    preprocessing: Preprocessing | TextPreprocessing = IMAGENET_PREPROCESSING


class TaskInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    data_kind: DataKind
    presets: Mapping[TrainPreset, PresetSpec] = {}  # empty: not trainable yet
    servable: bool = False  # the serving script in serve_client can serve its models


TASKS: Mapping[TaskType, TaskInfo] = {
    TaskType.IMAGE_CLASSIFICATION: TaskInfo(
        data_kind=DataKind.IMAGES,
        servable=True,
        presets={
            TrainPreset.SMALL: PresetSpec(base_model=TrainBaseModel(repo_id="facebook/convnext-tiny-224", revision="6166b7613034066690a621d8bf25ffdf181a34f0"), learning_rate=1e-4),
            TrainPreset.MEDIUM: PresetSpec(base_model=TrainBaseModel(repo_id="facebook/convnext-small-224", revision="e0df91dcc78dbc0f56be8fbecff706128aee7c28"), learning_rate=1e-4),
            TrainPreset.LARGE: PresetSpec(base_model=TrainBaseModel(repo_id="facebook/convnext-base-224", revision="8550c1da008c99b90c9c42f1d4e1cb650d7a8c05"), learning_rate=5e-5),
        },
    ),
    TaskType.OBJECT_DETECTION: TaskInfo(
        data_kind=DataKind.IMAGES,
        servable=True,
        presets={
            TrainPreset.SMALL: PresetSpec(base_model=TrainBaseModel(repo_id="PekingU/rtdetr_v2_r18vd", revision="5650961749fa93567c0d46fc7f43ea4f9e914107"), learning_rate=1e-4),
            TrainPreset.MEDIUM: PresetSpec(base_model=TrainBaseModel(repo_id="PekingU/rtdetr_v2_r34vd", revision="5d60d8fb5eb2a3ee5575bf200ecbce4e2cfa420c"), learning_rate=1e-4),
            TrainPreset.LARGE: PresetSpec(base_model=TrainBaseModel(repo_id="PekingU/rtdetr_v2_r50vd", revision="282494075698cab9faa1096ae26856890030c817"), learning_rate=5e-5),
        },
    ),
    TaskType.TEXT_CLASSIFICATION: TaskInfo(
        data_kind=DataKind.TEXT,
        presets={
            TrainPreset.SMALL: PresetSpec(base_model=TrainBaseModel(repo_id="distilbert/distilbert-base-uncased", revision="12040accade4e8a0f71eabdb258fecc2e7e948be"), learning_rate=2e-5, preprocessing=DEFAULT_TEXT_PREPROCESSING),
            TrainPreset.MEDIUM: PresetSpec(base_model=TrainBaseModel(repo_id="google-bert/bert-base-uncased", revision="86b5e0934494bd15c9632b12f734a8a67f723594"), learning_rate=2e-5, preprocessing=DEFAULT_TEXT_PREPROCESSING),
            TrainPreset.LARGE: PresetSpec(base_model=TrainBaseModel(repo_id="FacebookAI/roberta-base", revision="e2da8e2f811d1448a5b465c236feacd80ffbac7b"), learning_rate=1e-5, preprocessing=DEFAULT_TEXT_PREPROCESSING),
        },
    ),
    TaskType.TABULAR_CLASSIFICATION: TaskInfo(data_kind=DataKind.TABULAR),
    TaskType.TABULAR_REGRESSION: TaskInfo(data_kind=DataKind.TABULAR),
}


TRAINABLE: tuple[TaskType, ...] = tuple(t for t, i in TASKS.items() if i.presets)  # job_spec.TrainTask is these
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
    return task in TASKS and bool(TASKS[task].presets)


def servable(task: TaskType) -> bool:
    return task in TASKS and TASKS[task].servable


def preset_spec(task: TaskType, preset: TrainPreset) -> PresetSpec:
    return TASKS[task].presets[preset]


def base_model(task: TaskType, preset: TrainPreset) -> TrainBaseModel:
    return preset_spec(task, preset).base_model


def require_supported_task_type(task_type: TaskType) -> None:
    if not supported(task_type):
        raise Problem(422, "task_type_not_supported", f"task type '{task_type}' is not supported yet")
