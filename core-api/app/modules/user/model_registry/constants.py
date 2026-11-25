"""Named values for the model registry."""
from enum import StrEnum

from app.integrations.data_plane import MODEL_KEY, MODEL_META_KEY

NOTHING_TO_UPDATE_DETAIL = "nothing to update"


class DownloadFile(StrEnum):
    """The files of a version a project member may download (the names under the run's `model/` artifacts)."""

    MODEL = MODEL_KEY
    META = MODEL_META_KEY


DOWNLOAD_MEDIA_TYPES = {DownloadFile.MODEL: "application/octet-stream", DownloadFile.META: "application/json"}


class AuditAction(StrEnum):
    DOWNLOAD = "download"


class EntityType(StrEnum):
    MODEL_VERSION = "model_version"


class ProblemCode(StrEnum):
    NOTHING_TO_UPDATE = "nothing_to_update"
