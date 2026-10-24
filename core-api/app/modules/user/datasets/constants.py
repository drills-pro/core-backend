from datetime import timedelta
from enum import StrEnum

from app.core.enums import TABULAR_TASKS, ImportSource  # noqa: F401 - re-exported for the module and its tests


class ImportStatus(StrEnum):
    """Import lifecycle. Mirrors the dataset_imports_status_check constraint."""

    AWAITING_UPLOAD = "awaiting_upload"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    COMMITTING = "committing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    DISCARDED = "discarded"


DATASET_RETENTION_DAYS = 7  # a soft-deleted dataset stays restorable this long, then a sweep removes it


S = ImportStatus
# Legal import transitions. Everything the API does is a compare-and-set along one of these edges; the
# reconciler (plan step 8) drives queued -> running -> awaiting_review|failed and the expiries.
TRANSITIONS: dict[ImportStatus, frozenset[ImportStatus]] = {
    S.AWAITING_UPLOAD: frozenset({S.QUEUED, S.CANCELLED, S.EXPIRED}),
    S.QUEUED: frozenset({S.RUNNING, S.FAILED, S.CANCELLED, S.AWAITING_UPLOAD}),  # back to awaiting_upload: the job never started
    S.RUNNING: frozenset({S.AWAITING_REVIEW, S.FAILED, S.CANCELLED}),
    S.AWAITING_REVIEW: frozenset({S.COMMITTING, S.DISCARDED, S.EXPIRED}),
    # committing stays committing when a commit fails; committing again resumes it (the lakeFS commit is idempotent).
    # Back to review only when the lease ran out (the process died): the sweep or the next import's claim does it.
    S.COMMITTING: frozenset({S.SUCCEEDED, S.AWAITING_REVIEW}),
    S.SUCCEEDED: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
    S.EXPIRED: frozenset(),
    S.DISCARDED: frozenset(),
}
# Statuses that count against the per-tenant cap (mirrors the dataset_imports_active_idx predicate).
ACTIVE_STATUSES = (S.AWAITING_UPLOAD, S.QUEUED, S.RUNNING, S.AWAITING_REVIEW, S.COMMITTING)

COMMIT_LEASE_GRACE = timedelta(seconds=30)  # added to the lakeFS commit timeout: how long a committing import is "in progress"
IDEMPOTENCY_HEADER = "Idempotency-Key"
IMPORT_UPLOAD_TTL = timedelta(hours=24)  # an upload nobody started is expired after this
IMPORT_REVIEW_TTL = timedelta(days=7)  # an import nobody committed or discarded is expired after this
SYSTEM_ACTOR = "system"  # audit actor for what the background sweeps do


class RayJobStatus(StrEnum):
    """Ray's job states (the dashboard API)."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


class AuditAction(StrEnum):
    DELETE = "delete"
    RESTORE = "restore"
    PUBLISH = "publish"
    UNPUBLISH = "unpublish"
    IMPORT_CREATED = "import_created"
    IMPORT_COMMITTED = "import_committed"
    IMPORT_DISCARDED = "import_discarded"
    IMPORT_EXPIRED = "import_expired"


class EntityType(StrEnum):
    DATASET = "dataset"
    DATASET_IMPORT = "dataset_import"


class ProblemCode(StrEnum):
    NOT_DELETED = "dataset_not_deleted"
    STALE_BASE = "stale_base"
    INVALID_STATE = "invalid_import_state"
    TOO_MANY_IMPORTS = "too_many_active_imports"
    TASK_TYPE_MISMATCH = "task_type_mismatch"
    INVALID_PARAMS = "invalid_import_params"
    BASE_REQUIRED = "base_version_required"
    TASK_TYPE_MISSING = "project_task_type_missing"
    IDEMPOTENCY_MISMATCH = "idempotency_key_reused"


BASE_REQUIRED_DETAIL = "this dataset already has versions; set base_version_id to the latest one (there is no overwrite mode)"
NOT_DELETED_DETAIL = "this dataset is not pending deletion, or its retention window has passed"
STALE_BASE_DETAIL = "the base version is no longer the latest; start a new import on the latest version"

PARAM_MAX_LENGTH = 255  # longest repo id, revision, column or target name an import accepts
# One path segment of a Hugging Face repo id or ref name: it needs a non-dot character, so `.` and `..` are out and a
# value cannot climb out of the /datasets/<repo>/... URLs the ingest job builds from it.
HF_SEGMENT = r"\.*[\w-][\w.-]*"
HF_REPO_PATTERN = rf"^({HF_SEGMENT}/)?{HF_SEGMENT}$"
HF_REVISION_PATTERN = rf"^{HF_SEGMENT}(/{HF_SEGMENT})*$"

PREVIEW_DEFAULT_LIMIT = 20
PREVIEW_MAX_LIMIT = 100
