"""The report schema is the ingest job's contract: the checked-in JSON Schema must match the model."""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.integrations.data_plane import layout, report
from app.integrations.data_plane.report import Issue, IssueCode, ImportReport, Outcome, Severity

SCHEMA_FILE = Path(report.__file__).with_name("report.schema.json")


def test_checked_in_json_schema_matches_the_model():
    assert json.loads(SCHEMA_FILE.read_text()) == ImportReport.model_json_schema(), (
        "regenerate report.schema.json: python -m app.integrations.data_plane.report"
    )


def test_minimal_failed_report_round_trips():
    issue = Issue(code=IssueCode.ARCHIVE_SYMLINK, severity=Severity.ERROR, path="a/b", message="symlink")
    parsed = ImportReport.model_validate_json(ImportReport(outcome=Outcome.FAILED, issues=[issue]).model_dump_json())
    assert parsed.stats is None and parsed.issues == [issue]


def test_unknown_fields_and_codes_are_rejected():
    with pytest.raises(ValidationError):
        ImportReport.model_validate({"outcome": "ok", "surprise": 1})
    with pytest.raises(ValidationError):
        ImportReport.model_validate({"outcome": "failed", "issues": [{"code": "nope", "severity": "error", "message": "x"}]})


def test_issue_list_is_capped():
    issues = [{"code": "corrupt_image", "severity": "error", "message": "x"}] * (report.MAX_REPORT_ISSUES + 1)
    with pytest.raises(ValidationError):
        ImportReport.model_validate({"outcome": "failed", "issues": issues})


def test_staging_prefix_is_tenant_dataset_import():
    assert layout.staging_prefix("t", "d", "i") == "imports/t/d/i/"


def test_a_string_license_becomes_a_list():
    assert ImportReport.model_validate({"outcome": "failed", "license": "mit"}).license == ["mit"]
    assert ImportReport.model_validate({"outcome": "failed", "license": ["cc-by-4.0", "mit"]}).license == ["cc-by-4.0", "mit"]
