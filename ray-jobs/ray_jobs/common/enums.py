"""Closed sets of the job: row and params keys, Hugging Face and COCO keys, issue codes."""
from enum import StrEnum


class Col(StrEnum):
    """Columns of index.parquet and keys of one row; INDEX_COLUMNS mirrors layout.INDEX_COLUMNS (drift-tested)."""

    PATH = "path"
    SIZE = "size"
    SHA256 = "sha256"
    LABEL = "label"
    BOXES = "boxes"
    SPLIT = "split"


class Box(StrEnum):
    """Keys of one detection box; BOX_FIELDS mirrors layout.BOX_FIELDS (drift-tested)."""

    CATEGORY = "category"
    X = "x"
    Y = "y"
    WIDTH = "width"
    HEIGHT = "height"


INDEX_COLUMNS = tuple(Col)
BOX_FIELDS = tuple(Box)


class Param(StrEnum):
    """Keys of the params JSON core-api builds in data_plane/ingest.py (drift-tested)."""

    BUCKET = "bucket"
    PREFIX = "prefix"
    SOURCE = "source"
    TASK = "task"
    TARGET = "target"
    SPLIT = "split"
    LIMITS = "limits"
    BASE = "base"
    HF = "hf"
    INDEX_URL = "index_url"
    DATA_URL = "data_url"
    SCHEMA_URL = "schema_url"
    REPO = "repo"
    REVISION = "revision"
    COLUMN = "column"


class Hf(StrEnum):
    """Hugging Face API, dataset card and record keys."""

    CARD_DATA = "cardData"
    SIBLINGS = "siblings"
    RFILENAME = "rfilename"
    SIZE = "size"
    SHA = "sha"
    LICENSE = "license"
    TASK_CATEGORIES = "task_categories"
    DATASET_INFO = "dataset_info"
    FEATURES = "features"
    NAME = "name"
    DTYPE = "dtype"
    CLASS_LABEL = "class_label"
    NAMES = "names"
    SEQUENCE = "sequence"
    IMAGE = "image"
    BYTES = "bytes"
    BBOX = "bbox"
    CATEGORIES = "categories"
    CATEGORY = "category"


class Coco(StrEnum):
    """Keys of a COCO annotations document."""

    IMAGES = "images"
    ANNOTATIONS = "annotations"
    CATEGORIES = "categories"
    ID = "id"
    NAME = "name"
    FILE_NAME = "file_name"
    IMAGE_ID = "image_id"
    CATEGORY_ID = "category_id"
    BBOX = "bbox"


class JobKind(StrEnum):
    """Mirrors job_spec.JobKind (drift-tested); only INGEST is run by this package."""

    INGEST = "ingest"


class Source(StrEnum):
    """Mirrors ImportSource (drift-tested)."""

    UPLOAD = "upload"
    HUGGINGFACE = "huggingface"


class Outcome(StrEnum):
    """Mirrors report.Outcome (drift-tested)."""

    OK = "ok"
    FAILED = "failed"


class ColumnType(StrEnum):
    """Mirrors layout.ColumnType, the schema.json type vocabulary (drift-tested)."""

    BOOL = "bool"
    INT64 = "int64"
    FLOAT64 = "float64"
    STRING = "string"


class FeatureKind(StrEnum):
    """Kind of one Hugging Face card feature."""

    IMAGE = "image"
    CLASS_LABEL = "class_label"
    NUMBER = "number"
    STRING = "string"
    BOOL = "bool"
    OBJECTS = "objects"
    OTHER = "other"


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class Code(StrEnum):
    """Subset of report.IssueCode this job emits; tests assert every value is in the schema."""

    ARCHIVE_UNSAFE_PATH = "archive_unsafe_path"
    ARCHIVE_SYMLINK = "archive_symlink"
    ARCHIVE_TOO_LARGE = "archive_too_large"
    ARCHIVE_TOO_MANY_FILES = "archive_too_many_files"
    ARCHIVE_TOO_DEEP = "archive_too_deep"
    ARCHIVE_DUPLICATE_PATH = "archive_duplicate_path"
    PATH_TOO_LONG = "path_too_long"
    UNSUPPORTED_EXTENSION = "unsupported_extension"
    CORRUPT_IMAGE = "corrupt_image"
    IMAGE_TOO_LARGE = "image_too_large"
    LABEL_CASE_COLLISION = "label_case_collision"
    NO_DATA = "no_data"
    COCO_INVALID = "coco_invalid"
    CSV_NO_HEADER = "csv_no_header"
    CSV_DUPLICATE_COLUMNS = "csv_duplicate_columns"
    CSV_TOO_FEW_ROWS = "csv_too_few_rows"
    CSV_TOO_MANY_ROWS = "csv_too_many_rows"
    CSV_TOO_MANY_COLUMNS = "csv_too_many_columns"
    CSV_ROW_TOO_LONG = "csv_row_too_long"
    TARGET_MISSING = "target_missing"
    TARGET_TYPE_MISMATCH = "target_type_mismatch"
    TEXT_COLUMN_MISSING = "text_column_missing"
    SCHEMA_MISMATCH = "schema_mismatch"
    MERGE_CONFLICT = "merge_conflict"
    DUPLICATE_SKIPPED = "duplicate_skipped"
    HF_CARD_MISMATCH = "hf_card_mismatch"
    HF_SCHEMA_MISMATCH = "hf_schema_mismatch"
    HF_TOO_LARGE = "hf_too_large"
    HF_NO_DATA_FILES = "hf_no_data_files"
    JOB_TIMEOUT = "job_timeout"
    INTERNAL_ERROR = "internal_error"
