"""The closed sets the packages share: job kinds, the report status words, the index.parquet vocabulary."""
from enum import StrEnum


class JobKind(StrEnum):
    """Mirrors job_spec.JobKind (drift-tested). Each package runs one kind and refuses the others."""

    INGEST = "ingest"
    TRAIN = "train"
    PREDICT_BATCH = "predict_batch"


class Status(StrEnum):
    """`status` of a train or predict_batch report.json (core-api TrainOutcome). The ingest report has its own `outcome` words."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Col(StrEnum):
    """Columns of index.parquet and keys of one row; tuple(Col) mirrors layout.INDEX_COLUMNS (drift-tested)."""

    PATH = "path"
    SIZE = "size"
    SHA256 = "sha256"
    LABEL = "label"
    BOXES = "boxes"
    SPLIT = "split"


class Box(StrEnum):
    """Keys of one detection box; tuple(Box) mirrors layout.BOX_FIELDS (drift-tested)."""

    CATEGORY = "category"
    X = "x"
    Y = "y"
    WIDTH = "width"
    HEIGHT = "height"


class Split(StrEnum):
    """Mirrors layout.Split (drift-tested)."""

    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"
