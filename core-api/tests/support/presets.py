"""A task_presets row as the database returns it, with the values the seed migration gives the image tasks."""
from datetime import UTC, datetime

from app.core.enums import TaskType
from app.core.presets import TaskPreset, _model

IMAGENET = {"mean": [0.485, 0.456, 0.406], "std": [0.229, 0.224, 0.225], "rescale": 1 / 255, "resample": "bicubic"}
LIMITS = {
    "epochs": {"default": 30, "min": 1, "max": 500},
    "batch_size": {"default": 16, "min": 1, "max": 512},
    "image_size": {"default": 224, "min": 32, "max": 1536},
}
REVISION = "6166b7613034066690a621d8bf25ffdf181a34f0"


def preset_row(task=TaskType.IMAGE_CLASSIFICATION, name="small", **over):
    return {
        "task": task, "name": name, "base_model_repo": "facebook/convnext-tiny-224", "base_model_revision": REVISION,
        "learning_rate": 1e-4, "preprocessing": IMAGENET, "cpu": 8, "memory_bytes": 25769803776, "gpu": 1,
        "max_runtime_s": 14400, "hyperparameters": LIMITS, "updated_by": "migration", "updated_at": datetime.now(UTC), **over,
    }


def preset(task=TaskType.IMAGE_CLASSIFICATION, name="small", **over) -> TaskPreset:
    return _model(preset_row(task, name, **over))
