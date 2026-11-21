from datetime import timedelta
from enum import StrEnum

from app.core.enums import ImportSource  # noqa: F401 - re-exported for the module and its tests


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
IMPORT_UPLOAD_TTL = timedelta(hours=24)  # an upload nobody started is expired after this
IMPORT_REVIEW_TTL = timedelta(days=7)  # an import nobody committed or discarded is expired after this
SYSTEM_ACTOR = "system"  # audit actor for what the background sweeps do


class DataCategory(StrEnum):
    """What the customer declares a dataset holds (planning/18). Stored only. Mirrors datasets_data_categories_check."""

    PERSONAL_DATA = "personal_data"
    BIOMETRIC = "biometric"
    OTHER_SENSITIVE = "other_sensitive"


class AuditAction(StrEnum):
    CREATE = "create"
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
    START_FAILED = "import_start_failed"


BASE_REQUIRED_DETAIL = "this dataset already has versions; set base_version_id to the latest one (there is no overwrite mode)"
NOT_DELETED_DETAIL = "this dataset is not pending deletion, or its retention window has passed"
TOO_MANY_IMPORTS_DETAIL = "too many imports in progress, wait for one to finish"
IMPORT_LOCK_PREFIX = "imports:"  # advisory-lock key prefix: serialises the cap check and insert per tenant
START_FAILED_DETAIL = "the import could not be started; it is unchanged, try again shortly"
STALE_BASE_DETAIL = "the base version is no longer the latest; start a new import on the latest version"

PARAM_MAX_LENGTH = 255  # longest repo id, revision, column or target name an import accepts
# One path segment of a Hugging Face repo id or ref name: it needs a non-dot character, so `.` and `..` are out and a
# value cannot climb out of the /datasets/<repo>/... URLs the ingest job builds from it.
HF_SEGMENT = r"\.*[\w-][\w.-]*"
HF_REPO_PATTERN = rf"^({HF_SEGMENT}/)?{HF_SEGMENT}$"
HF_REVISION_PATTERN = rf"^{HF_SEGMENT}(/{HF_SEGMENT})*$"

PREVIEW_DEFAULT_LIMIT = 20
PREVIEW_MAX_LIMIT = 100


PIPELINE_SERVICE_NAME = "data-pipeline"  # OTel service.name of the import pipeline's own log stream (pipeline_log.py)


class PipelineEvent(StrEnum):
    """The `pipeline.event` attribute on the import pipeline log records (pipeline_log.py)."""

    STARTED = "pipeline_started"  # the ingest job was submitted and its id saved
    COMPLETED = "pipeline_completed"  # the job's report is ok; the import awaits review
    FAILED = "pipeline_failed"  # the job's report failed, or the job ended without one, or ran out of time
    FILE_EXTRACTION_FAILED = "file_extraction_failed"  # archive_* issues: the zip is unsafe or over a limit
    FILE_VALIDATION_FAILED = "file_validation_failed"  # the data does not fit the task (CSV, images, COCO, Hugging Face)
    DATA_TRANSFORMATION_FAILED = "data_transformation_failed"  # merging with the base version failed
    DATA_STORAGE_FAILED = "data_storage_failed"  # committing the reviewed import as a dataset version failed


class PipelineAttribute(StrEnum):
    """Attribute keys on the import pipeline log records. Loki turns the dots into underscores."""

    EVENT = "pipeline.event"  # PipelineEvent
    TENANT = "tenant_id"
    PROJECT = "project_id"
    DATASET = "dataset_id"
    IMPORT = "import_id"
    RAY_JOB = "ray_job_id"
    ISSUE_CODES = "pipeline.issue_codes"  # the report's distinct issue codes behind the event, comma-separated
    ISSUE_COUNT = "pipeline.issue_count"  # issues behind the event: ERRORs on a failure, warnings on completed
    ERROR_TYPE = "pipeline.error_type"  # exception class behind data_storage_failed
