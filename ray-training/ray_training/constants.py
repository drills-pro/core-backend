"""Closed sets and named values of the trainer. Layout and slugs mirror core-api / ray_jobs (test_spec.py checks the spec)."""
from enum import StrEnum

SPEC_VERSION = 1  # job spec version this code reads (core-api app/integrations/job_spec.py)
REPORT_SCHEMA_VERSION = 1
TRAINER_VERSION = "0.1.0"
DEFAULT_EPOCHS, DEFAULT_BATCH_SIZE, DEFAULT_IMAGE_SIZE, DEFAULT_SEED = 30, 16, 224, 42  # core-api job_spec.py defaults
NO_ALARM = 0  # signal.alarm(0) cancels
HTTP_TIMEOUT_SECONDS = 60
LOCAL_GPUS_ENV = "TRAIN_NUM_GPUS"  # CI sets 0 so the remote task needs no GPU; production leaves it unset (= 1)
DEFAULT_TASK_GPUS = 1
CONFUSION_TOP_K = 5
ONNX_OPSET = 17
MAP_IOU_THRESHOLDS = tuple(0.5 + 0.05 * i for i in range(10))  # COCO: 0.50..0.95
MAP_RECALL_POINTS = 101
SPLIT_BUCKETS = 100  # hash split: bucket = int(sha256[:8], 16) % 100
SPLIT_TRAIN_BELOW, SPLIT_VALIDATION_BELOW = 80, 90  # 80 / 10 / 10
HASH_HEX_CHARS = 8

# key names under the run prefix
REPORT_KEY = "report.json"
MODEL_KEY = "model.onnx"
MODEL_MANIFEST_KEY = "model.json"


class Task(StrEnum):
    """Mirrors app.core.enums.TaskType (the two this trainer runs)."""

    IMAGE_CLASSIFICATION = "image-classification"
    OBJECT_DETECTION = "object-detection"


class Preset(StrEnum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class Resample(StrEnum):
    """Mirrors app.core.task_catalog.Resample; the names are PIL.Image.Resampling members."""

    BILINEAR = "bilinear"
    BICUBIC = "bicubic"


class Split(StrEnum):
    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"


class Col(StrEnum):
    """Columns of index.parquet (ray_jobs.common.enums.Col)."""

    PATH = "path"
    SIZE = "size"
    SHA256 = "sha256"
    LABEL = "label"
    BOXES = "boxes"
    SPLIT = "split"


class Box(StrEnum):
    CATEGORY = "category"
    X = "x"
    Y = "y"
    WIDTH = "width"
    HEIGHT = "height"


class FailCode(StrEnum):
    OOM = "oom"
    BAD_DATA = "bad_data"
    UNSUPPORTED = "unsupported"
    TIMEOUT = "timeout"
    INTERNAL = "internal"


class Outcome(StrEnum):
    SUCCEEDED = "succeeded"  # core-api TrainOutcome
    FAILED = "failed"


class JobKind(StrEnum):
    TRAIN = "train"
