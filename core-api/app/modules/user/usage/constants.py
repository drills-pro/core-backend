"""Named values for usage events."""
from enum import StrEnum


class UsageResource(StrEnum):
    """Mirrors the usage_events.resource_type CHECK."""

    TRAINING = "training"
    STORAGE_DATASET = "storage_dataset"
    STORAGE_MODEL = "storage_model"
    INFERENCE = "inference"


class UsageUnit(StrEnum):
    GPU_SECONDS = "gpu_seconds"
    REQUESTS = "requests"
