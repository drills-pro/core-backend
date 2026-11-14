"""The import pipeline's log records: their own OTLP stream (service.name data-pipeline), apart from the Ray job logs.

The ingest job's own output stays in Ray (driver logs, `ray job logs`) and is not this stream: nothing in ray_jobs
writes these records. core-api writes them at the points it sees the pipeline move:

    pipeline_started            start_import submitted the job and saved its id
    pipeline_completed          reconcile read an ok report; the import awaits review
    file_*/data_*_failed        reconcile read a failed report: one record per failed stage (STAGE_EVENTS), then
    pipeline_failed             ... this one (also for a timeout or a job that ended without a report)
    data_storage_failed         commit_import could not store the reviewed import as a dataset version

Records are written only by the replica whose compare-and-set moved the import, so each is logged once. They go to
OTLP only (loki_log.ship), never to stdout.

In Grafana: {service_name="data-pipeline"} | pipeline_event="pipeline_failed" | import_id="<id>".
"""
import logging
from collections.abc import Sequence

from app.core import loki_log
from app.integrations.data_plane import ImportReport, Issue, IssueCode, Severity
from app.modules.user.datasets.constants import PIPELINE_SERVICE_NAME
from app.modules.user.datasets.constants import PipelineAttribute as A
from app.modules.user.datasets.constants import PipelineEvent as E

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)  # the root logger stays at WARNING; started and completed are INFO

LEVELS = {
    E.STARTED: logging.INFO,
    E.COMPLETED: logging.INFO,
    E.FAILED: logging.ERROR,
    E.FILE_EXTRACTION_FAILED: logging.ERROR,
    E.FILE_VALIDATION_FAILED: logging.WARNING,  # the user's data is wrong, not the platform
    E.DATA_TRANSFORMATION_FAILED: logging.ERROR,
    E.DATA_STORAGE_FAILED: logging.ERROR,
}

_EXTRACTION = (
    IssueCode.ARCHIVE_UNSAFE_PATH, IssueCode.ARCHIVE_SYMLINK, IssueCode.ARCHIVE_TOO_LARGE, IssueCode.ARCHIVE_TOO_MANY_FILES,
    IssueCode.ARCHIVE_TOO_DEEP, IssueCode.ARCHIVE_DUPLICATE_PATH, IssueCode.PATH_TOO_LONG,
)
_VALIDATION = (
    IssueCode.UNSUPPORTED_EXTENSION, IssueCode.CORRUPT_IMAGE, IssueCode.IMAGE_TOO_LARGE, IssueCode.LABEL_CASE_COLLISION,
    IssueCode.NO_DATA, IssueCode.COCO_INVALID, IssueCode.CSV_NO_HEADER, IssueCode.CSV_DUPLICATE_COLUMNS,
    IssueCode.CSV_TOO_FEW_ROWS, IssueCode.CSV_TOO_MANY_ROWS, IssueCode.CSV_TOO_MANY_COLUMNS, IssueCode.CSV_ROW_TOO_LONG,
    IssueCode.TARGET_MISSING, IssueCode.TARGET_TYPE_MISMATCH, IssueCode.TEXT_COLUMN_MISSING, IssueCode.HF_CARD_MISMATCH,
    IssueCode.HF_SCHEMA_MISMATCH, IssueCode.HF_TOO_LARGE, IssueCode.HF_NO_DATA_FILES,
)
_TRANSFORMATION = (IssueCode.SCHEMA_MISMATCH, IssueCode.MERGE_CONFLICT, IssueCode.DUPLICATE_SKIPPED)
_NO_STAGE = (IssueCode.JOB_TIMEOUT, IssueCode.INTERNAL_ERROR)  # the job as a whole: pipeline_failed alone

# Every IssueCode, so a new one cannot go unclassified (tested).
STAGE_EVENTS: dict[IssueCode, E | None] = {
    **dict.fromkeys(_EXTRACTION, E.FILE_EXTRACTION_FAILED),
    **dict.fromkeys(_VALIDATION, E.FILE_VALIDATION_FAILED),
    **dict.fromkeys(_TRANSFORMATION, E.DATA_TRANSFORMATION_FAILED),
    **dict.fromkeys(_NO_STAGE),
}
_REPORT_STAGES = (E.FILE_EXTRACTION_FAILED, E.FILE_VALIDATION_FAILED, E.DATA_TRANSFORMATION_FAILED)


def setup() -> None:
    """Called once by each composition root that runs the pipeline (main.py, worker.py)."""
    loki_log.ship(PIPELINE_SERVICE_NAME, logger)


def _emit(event: E, row: dict, issues: Sequence[Issue] = (), **attrs: str) -> None:
    extra = {
        A.EVENT: event.value, A.TENANT: str(row["tenant_id"]), A.PROJECT: str(row["project_id"]),
        A.DATASET: str(row["dataset_id"]), A.IMPORT: str(row["id"]), **attrs,
    }
    if row.get("ray_job_id"):
        extra[A.RAY_JOB] = row["ray_job_id"]
    codes = ",".join(sorted({i.code.value for i in issues}))
    if issues:
        extra[A.ISSUE_CODES], extra[A.ISSUE_COUNT] = codes, len(issues)
    logger.log(LEVELS[event], "%s: import %s %s", event.value, row["id"], codes, extra=extra)


def started(row: dict) -> None:
    _emit(E.STARTED, row)


def completed(row: dict, report: ImportReport) -> None:
    """The report is ok; its warnings (skipped duplicates, ignored files) ride along."""
    _emit(E.COMPLETED, row, report.issues)


def failed(row: dict, report: ImportReport) -> None:
    errors = [i for i in report.issues if i.severity is Severity.ERROR]
    for stage in _REPORT_STAGES:
        if behind := [i for i in errors if STAGE_EVENTS[i.code] is stage]:
            _emit(stage, row, behind)
    _emit(E.FAILED, row, errors)


def storage_failed(row: dict, error: Exception) -> None:
    """The commit did not land; the import stays committable, so this may be followed by a successful retry."""
    _emit(E.DATA_STORAGE_FAILED, row, **{A.ERROR_TYPE: type(error).__name__})
