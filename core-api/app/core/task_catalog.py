"""Shapes shared by the train job and the presets: the kinds of data, the column names of a text dataset, the base model pin
and the two preprocessing models. What the platform can do with each task type (its data kind, trainer, whether it can be
served) is rows of `task_types` (app/core/task_types.py), changed through /v1/admin without a deploy; what a trainable task
trains with is rows of `task_presets` (app/core/presets.py).

A model that normalises differently (CLIP, 0.5/0.5 ViTs) sets its own `Preprocessing` in its preset row. A text task's
preprocessing is `TextPreprocessing`.
"""
from enum import StrEnum

from app.core.enums import TaskType

from pydantic import BaseModel, ConfigDict, Field



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


class LrSchedule(StrEnum):
    """Learning rate over a run's steps, after a linear warmup: held, or cosine-decayed to zero. Mirrors task_presets_lr_schedule_check."""

    CONSTANT = "constant"
    COSINE = "cosine"


class Augmentation(StrEnum):
    """Train-split-only image recipe. Mirrors task_presets_augmentation_check; the trainer mirrors it (contract-tested)."""

    NONE = "none"
    FLIP_CROP = "flip_crop"  # classification: random horizontal flip and a reflect-padded random crop
    FLIP = "flip"  # detection: random horizontal flip, boxes mirrored with it


# What each task's trainer implements; text has no recipe.
TASK_AUGMENTATIONS: dict[TaskType, frozenset[Augmentation]] = {
    TaskType.IMAGE_CLASSIFICATION: frozenset({Augmentation.NONE, Augmentation.FLIP_CROP}),
    TaskType.OBJECT_DETECTION: frozenset({Augmentation.NONE, Augmentation.FLIP}),
    TaskType.TEXT_CLASSIFICATION: frozenset({Augmentation.NONE}),
}


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
