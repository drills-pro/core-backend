from enum import StrEnum


class TaskType(StrEnum):
    """Mirrors the projects.task_type CHECK constraint. FR4 (02-org-team-project-model.md): set at
    creation, one of a fixed vocabulary, or the legacy escape hatch for pre-Phase-2 data."""

    IMAGE_CLASSIFICATION = "image_classification"
    OBJECT_DETECTION = "object_detection"
    TABULAR_CLASSIFICATION = "tabular_classification"
    TEXT_CLASSIFICATION = "text_classification"
    LEGACY_UNTYPED = "legacy_untyped"
