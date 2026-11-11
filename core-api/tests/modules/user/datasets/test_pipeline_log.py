"""Import pipeline log records (pipeline_log.py): which events go to the data-pipeline stream, at which level."""
import logging
import uuid

import pytest

from app.core import loki_log
from app.integrations.data_plane.report import ImportReport, Issue, IssueCode, Outcome, Severity
from app.modules.user.datasets import pipeline_log
from app.modules.user.datasets.constants import PIPELINE_SERVICE_NAME
from app.modules.user.datasets.constants import PipelineAttribute as A
from app.modules.user.datasets.constants import PipelineEvent as E

ROW = {"id": uuid.uuid4(), "tenant_id": uuid.uuid4(), "project_id": uuid.uuid4(), "dataset_id": uuid.uuid4(), "ray_job_id": "job"}


def pipeline_events(caplog) -> list[tuple[str, str, str | None]]:
    """(level, event, issue codes) of every pipeline record, in order."""
    return [(r.levelname, getattr(r, A.EVENT), getattr(r, A.ISSUE_CODES, None))
            for r in caplog.records if r.name == pipeline_log.logger.name]


def failed_report(*codes: IssueCode, warnings: tuple[IssueCode, ...] = ()) -> ImportReport:
    issues = [Issue(code=c, severity=Severity.ERROR, message="x") for c in codes]
    issues += [Issue(code=c, severity=Severity.WARNING, message="x") for c in warnings]
    return ImportReport(outcome=Outcome.FAILED, issues=issues)


def test_every_issue_code_has_a_stage_decision_and_every_event_a_level():
    assert set(pipeline_log.STAGE_EVENTS) == set(IssueCode)
    assert set(pipeline_log.LEVELS) == set(E)


def test_levels_follow_the_event_table():
    assert {e.value: logging.getLevelName(level) for e, level in pipeline_log.LEVELS.items()} == {
        "pipeline_started": "INFO", "pipeline_completed": "INFO", "pipeline_failed": "ERROR",
        "file_extraction_failed": "ERROR", "file_validation_failed": "WARNING",
        "data_transformation_failed": "ERROR", "data_storage_failed": "ERROR",
    }


def test_the_pipeline_is_its_own_stream(monkeypatch):
    shipped = []
    monkeypatch.setattr(loki_log, "ship", lambda name, *loggers: shipped.append((name, loggers)))
    pipeline_log.setup()
    assert shipped == [(PIPELINE_SERVICE_NAME, (pipeline_log.logger,))]
    assert PIPELINE_SERVICE_NAME != "core-backend"


def test_started_carries_the_ids(caplog):
    pipeline_log.started(ROW)
    (record,) = [r for r in caplog.records if r.name == pipeline_log.logger.name]
    assert getattr(record, A.EVENT) == E.STARTED and record.levelname == "INFO"
    ids = (getattr(record, A.TENANT), getattr(record, A.PROJECT), getattr(record, A.DATASET), getattr(record, A.IMPORT))
    assert ids == (str(ROW["tenant_id"]), str(ROW["project_id"]), str(ROW["dataset_id"]), str(ROW["id"]))
    assert getattr(record, A.RAY_JOB) == "job"


def test_a_failure_logs_each_failed_stage_once_then_pipeline_failed(caplog):
    report = failed_report(IssueCode.ARCHIVE_SYMLINK, IssueCode.CORRUPT_IMAGE, IssueCode.CORRUPT_IMAGE,
                           IssueCode.MERGE_CONFLICT, warnings=(IssueCode.DUPLICATE_SKIPPED,))
    pipeline_log.failed(ROW, report)
    assert pipeline_events(caplog) == [
        ("ERROR", E.FILE_EXTRACTION_FAILED, "archive_symlink"),
        ("WARNING", E.FILE_VALIDATION_FAILED, "corrupt_image"),
        ("ERROR", E.DATA_TRANSFORMATION_FAILED, "merge_conflict"),
        ("ERROR", E.FAILED, "archive_symlink,corrupt_image,merge_conflict"),  # warnings are not why it failed
    ]
    validation = [r for r in caplog.records if getattr(r, A.EVENT, None) == E.FILE_VALIDATION_FAILED]
    assert getattr(validation[0], A.ISSUE_COUNT) == 2


@pytest.mark.parametrize("code", [IssueCode.JOB_TIMEOUT, IssueCode.INTERNAL_ERROR])
def test_a_timeout_or_crash_has_no_stage(caplog, code):
    pipeline_log.failed(ROW, failed_report(code))
    assert pipeline_events(caplog) == [("ERROR", E.FAILED, code.value)]


def test_a_storage_failure_names_the_error(caplog):
    pipeline_log.storage_failed(ROW, ConnectionError("lakefs down"))
    (record,) = [r for r in caplog.records if r.name == pipeline_log.logger.name]
    assert (record.levelname, getattr(record, A.EVENT), getattr(record, A.ERROR_TYPE)) == ("ERROR", E.DATA_STORAGE_FAILED, "ConnectionError")
