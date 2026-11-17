"""Closed sets and named values of the trainer contract. Layout mirrors core-api / ray_jobs (test_spec.py checks the spec)."""
from enum import StrEnum

CONTRACT = 1  # the contract a task bundle declares in manifest.json (`contract`); a bundle runs only on a ray_core serving it
SPEC_VERSION = 1  # job spec version this code reads (core-api app/integrations/job_spec.py)
REPORT_SCHEMA_VERSION = 1
TRAINER_VERSION = "0.1.0"
DEFAULT_EPOCHS, DEFAULT_BATCH_SIZE, DEFAULT_IMAGE_SIZE, DEFAULT_SEED = 30, 16, 224, 42  # core-api job_spec.py defaults
NO_ALARM = 0  # signal.alarm(0) cancels
HTTP_TIMEOUT_SECONDS = 60
EVENTS_TIMEOUT_SECONDS = 5  # a progress post that takes longer is dropped: it must never hold up an epoch
ROOT_RANK = 0  # the Ray Train worker that evaluates, exports and uploads
LOCAL_GPUS_ENV = "TRAIN_NUM_GPUS"  # CI sets 0 so the remote task needs no GPU; production leaves it unset (= 1)
DEFAULT_TASK_GPUS = 1
DEFAULT_WEIGHT_DECAY = 0.01  # AdamW's own default; a bundle exposes it as the weight_decay hyperparameter
TRAIN_ATTEMPTS = 2  # Ray Train's FailureConfig.max_failures: one failed attempt is resumed from its last checkpoint, a second ends the run
TRAIN_STATE_KEEP = 1  # checkpoints kept per run
STATE_FILE = "state.pt"  # the resume state inside a checkpoint
CONFUSION_TOP_K = 5
ONNX_OPSET = 17
SPLIT_BUCKETS = 100  # hash split: bucket = int(sha256[:8], 16) % 100
SPLIT_TRAIN_BELOW, SPLIT_VALIDATION_BELOW = 80, 90  # 80 / 10 / 10
HASH_HEX_CHARS = 8

# key names under the run prefix
REPORT_KEY = "report.json"
SMOKE_KEY = "smoke.json"  # written by the smoke job: {"ok": bool} (core-api layout.SMOKE_KEY)
MODEL_KEY = "model.onnx"
MODEL_MANIFEST_KEY = "model.json"
TRAIN_STATE_DIR = "train-state"  # Ray Train checkpoints: top-level <bucket>/train-state/<job_id>/, expired by a bucket lifecycle rule
TOKENIZER_KEY = "tokenizer.json"  # a text model is served with the tokenizer it was trained with

TEXT_COLUMN = "text"  # the columns of a text dataset's data.parquet (core-api task_catalog.TEXT_COLUMN, LABEL_COLUMN)
LABEL_COLUMN = "label"


class EventType(StrEnum):
    """The events core-api takes (app.modules.user.training.constants.EventType)."""

    PROGRESS = "progress"


class Resample(StrEnum):
    """Mirrors app.core.task_catalog.Resample; the names are PIL.Image.Resampling members."""

    BILINEAR = "bilinear"
    BICUBIC = "bicubic"


class LrSchedule(StrEnum):
    """Mirrors app.core.task_catalog.LrSchedule (contract-tested)."""

    CONSTANT = "constant"
    COSINE = "cosine"


class Augmentation(StrEnum):
    """Mirrors app.core.task_catalog.Augmentation (contract-tested). Applied to the train split only."""

    NONE = "none"
    FLIP_CROP = "flip_crop"
    FLIP = "flip"


FLIP_PROBABILITY = 0.5
CROP_PAD_DIVISOR = 8  # flip_crop pads each side by image_size / 8 before cropping back


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
