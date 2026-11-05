"""The import report the ingest job writes to `<staging prefix>/report.json` and core-api stores in
dataset_imports.report. The job has no core-api imports, so the contract is the JSON Schema checked in
next to this file (report.schema.json, kept in sync by a test); core-api validates with ImportReport.

An import with any ERROR issue has outcome `failed` and creates no version. WARNING issues (for example
skipped exact duplicates) do not block a commit and are shown in review.
"""
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

REPORT_SCHEMA_VERSION = 1
MAX_REPORT_ISSUES = 200  # the job keeps the first ones and counts the rest in `issues_omitted`


def _as_list(v: Any) -> Any:
    """A Hugging Face card's license is a list; reports written before the job normalised it carry a bare string."""
    return [v] if isinstance(v, str) else v


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Outcome(StrEnum):
    OK = "ok"
    FAILED = "failed"


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class IssueCode(StrEnum):
    # archive safety and limits
    ARCHIVE_UNSAFE_PATH = "archive_unsafe_path"  # absolute path or `..`
    ARCHIVE_SYMLINK = "archive_symlink"
    ARCHIVE_TOO_LARGE = "archive_too_large"  # upload, extracted size or ratio over the limit
    ARCHIVE_TOO_MANY_FILES = "archive_too_many_files"
    ARCHIVE_TOO_DEEP = "archive_too_deep"
    ARCHIVE_DUPLICATE_PATH = "archive_duplicate_path"  # two members (or labels normalising alike) land on one path
    PATH_TOO_LONG = "path_too_long"
    UNSUPPORTED_EXTENSION = "unsupported_extension"
    CORRUPT_IMAGE = "corrupt_image"
    IMAGE_TOO_LARGE = "image_too_large"  # bytes or dimensions
    # labels and annotations
    LABEL_CASE_COLLISION = "label_case_collision"
    NO_DATA = "no_data"
    COCO_INVALID = "coco_invalid"
    # tabular
    CSV_NO_HEADER = "csv_no_header"
    CSV_DUPLICATE_COLUMNS = "csv_duplicate_columns"
    CSV_TOO_FEW_ROWS = "csv_too_few_rows"
    CSV_TOO_MANY_ROWS = "csv_too_many_rows"
    CSV_TOO_MANY_COLUMNS = "csv_too_many_columns"
    CSV_ROW_TOO_LONG = "csv_row_too_long"  # more cells than the header has columns
    TARGET_MISSING = "target_missing"
    TARGET_TYPE_MISMATCH = "target_type_mismatch"
    TEXT_COLUMN_MISSING = "text_column_missing"  # a text task needs a string column named text
    # merge against the base version
    SCHEMA_MISMATCH = "schema_mismatch"
    MERGE_CONFLICT = "merge_conflict"
    DUPLICATE_SKIPPED = "duplicate_skipped"  # warning: same path and sha256 as an existing row
    # Hugging Face
    HF_CARD_MISMATCH = "hf_card_mismatch"
    HF_SCHEMA_MISMATCH = "hf_schema_mismatch"
    HF_TOO_LARGE = "hf_too_large"
    HF_NO_DATA_FILES = "hf_no_data_files"
    # job
    JOB_TIMEOUT = "job_timeout"
    INTERNAL_ERROR = "internal_error"


class Issue(_Model):
    code: IssueCode
    severity: Severity
    path: str | None = None  # archive member, column or category the issue is about
    message: str


class Stats(_Model):
    """What the new version contains (after the merge). Stored as dataset_versions.stats."""

    file_count: int = Field(ge=0)  # image objects; 0 for tabular
    total_bytes: int = Field(ge=0)
    row_count: int = Field(ge=0)  # index.parquet or data.parquet rows
    classes: dict[str, int] = {}  # label -> rows, classification and detection
    splits: dict[str, int] = {}  # Split value -> rows; rows without a split are not counted
    duplicates_skipped: int = Field(default=0, ge=0)


class ImportReport(_Model):
    schema_version: Literal[1] = REPORT_SCHEMA_VERSION
    outcome: Outcome
    stats: Stats | None = None  # absent when the import failed
    issues: list[Issue] = Field(default=[], max_length=MAX_REPORT_ISSUES)
    issues_omitted: int = Field(default=0, ge=0)
    revision: str | None = None  # Hugging Face commit sha the data was read at
    license: Annotated[list[str] | None, BeforeValidator(_as_list)] = None  # Hugging Face licence, recorded not enforced


if __name__ == "__main__":  # regenerate the checked-in schema: python -m app.integrations.data_plane.report
    import json
    from pathlib import Path

    Path(__file__).with_name("report.schema.json").write_text(json.dumps(ImportReport.model_json_schema(), indent=2) + "\n")
