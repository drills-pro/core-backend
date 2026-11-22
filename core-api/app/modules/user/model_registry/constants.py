"""Named values for the model registry."""
from enum import StrEnum

from app.integrations.data_plane import MODEL_KEY, MODEL_META_KEY, ModelStage

# req/06 FR2: registry stages map to customer-facing states. Production is the version a model's calls are answered with
# (planning/27): it says nothing a deployment's own status does not, so it never says "live" or "deployed".
CUSTOMER_STAGE_LABELS = {
    ModelStage.NONE: "internal only",
    ModelStage.STAGING: "candidate",
    ModelStage.PRODUCTION: "serving",
    ModelStage.ARCHIVED: "retired",
}
MODEL_NAME_PATTERN = r"^[A-Za-z0-9_-]{1,60}$"  # a registered model's name as the customer chooses it (MLflow's full name is namespaced)

NOTHING_TO_UPDATE_DETAIL = "nothing to update"
BAD_STAGE_DETAIL = f"stage must be one of {sorted(ModelStage)}"
LIVE_VERSION_DETAIL = "this version is serving; promote another version to replace it"
PROMOTION_IN_PROGRESS_DETAIL = "another version of this model is being promoted; wait for it to finish"
PROMOTION_EVENT_PROMOTED = "model.promoted"  # the outbox kinds of a promotion that finished and of one that failed (entity_id = the version)
PROMOTION_EVENT_FAILED = "model.promotion_failed"


class PromotionStatus(StrEnum):
    """Mirrors the model_versions.promotion_status CHECK constraint; no status = no promotion in flight."""

    PENDING = "pending"  # asked for; the worker deploys the version, then flips the stage
    FAILED = "failed"  # the deployment or the flip failed: stages are as they were, promotion_error says why


class DownloadFile(StrEnum):
    """The files of a version a project member may download (the names under the run's `model/` artifacts)."""

    MODEL = MODEL_KEY
    META = MODEL_META_KEY


DOWNLOAD_MEDIA_TYPES = {DownloadFile.MODEL: "application/octet-stream", DownloadFile.META: "application/json"}


class AuditAction(StrEnum):
    DOWNLOAD = "download"
    REGISTER = "register"
    PROMOTE = "promote"
    ROLLBACK = "rollback"  # a promotion to Production of a version older than the one it replaces
    SET_PUBLISH = "set_publish"


class EntityType(StrEnum):
    MODEL_VERSION = "model_version"


class ProblemCode(StrEnum):
    NOTHING_TO_UPDATE = "nothing_to_update"
    INVALID_STAGE = "invalid_stage"
    LIVE_VERSION = "live_version"
    PROMOTION_IN_PROGRESS = "promotion_in_progress"
