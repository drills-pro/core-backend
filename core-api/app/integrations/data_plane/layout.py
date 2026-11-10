"""The staging layout: the contract between core-api and the ingest job.

The job holds no lakeFS credential. It reads its input and writes its output under one prefix of the
staging bucket (settings.data_plane.staging_bucket), and nothing else:

    imports/<tenant_id>/<dataset_id>/<import_id>/        <- staging_prefix(), stored as dataset_imports.staging_key
        upload/source          the uploaded archive or CSV (the presigned PUT target); absent for Hugging Face
        report.json            ImportReport (report.py); always written, also when the import fails
        out/                   what the import ADDS to main (merged with the base when one is set)
            index.parquet      image tasks: the FULL merged index, one row per object, columns INDEX_COLUMNS
            objects/<path>     image tasks: only the NEW image files, at the path index.parquet names
            data.parquet       tabular tasks: the FULL merged rows, a nullable `split` column added by the job
            schema.json        tabular tasks: column names and inferred types

The job code itself is staged next to the imports, content-addressed: `jobs/ray-jobs-<sha256>.zip` (job_package.py).
It is not under `imports/`, so discarding or expiring an import never touches it.

`commit_staged` copies `out/` into the dataset repo's own storage (S3 CopyObject inside the object store, no bytes
through core-api), links each object on main and commits once. The version therefore does not depend on staging: the
whole `imports/<...>` prefix may be purged after the commit, or expire if the import is never committed. A commit
only adds or overwrites the paths it carries, so the base version's `objects/*` stay in the repo untouched, and the job
rewrites only the index/data/schema files whole. For a merge import the job reads the base index or data through a
presigned GET (core-api passes the URLs) and writes the merged result. `upload/` and `report.json` never reach lakeFS.
"""
from enum import StrEnum

from app.core.enums import Split  # noqa: F401 - lives in core so job_spec need not import this package; re-exported here

STAGING_ROOT = "imports"
TRAIN_ROOT = "trainings"
JOB_PACKAGE_ROOT = "jobs"
PREDICTIONS_ROOT = "predictions"  # payloads too large for a predictions row: <root>/<tenant>/<project>/<prediction>/
PREDICTION_INPUT_KEY = "input.json"
PREDICTION_OUTPUT_KEY = "output.json"  # written under <attempt>/, so a stale attempt never overwrites the current one
SMOKE_KEY = "smoke.json"  # what a package's smoke job writes under smoke_prefix: {"ok": bool}
UPLOAD_KEY = "upload/source"
REPORT_KEY = "report.json"
BATCH_PREDICTIONS_KEY = "predictions.jsonl"  # a predict_batch job's results, beside its report.json: one JSON line per item
OUT_PREFIX = "out/"
OBJECTS_PREFIX = "objects/"
INDEX_KEY = "index.parquet"
DATA_KEY = "data.parquet"
SCHEMA_KEY = "schema.json"
# A training run's prefix (train_prefix) holds, next to the imports tree:
#   manifest.json      written by core-api before the job starts: {index path: presigned GET}
#   report.json        TrainReport (train_report.py); always written by the job, also when training fails
#   model.onnx, model.json   the model and its description (success only)
#   tokenizer.json           a text model's tokenizer, beside the model
MANIFEST_KEY = "manifest.json"
MODEL_KEY = "model.onnx"
MODEL_META_KEY = "model.json"
TOKENIZER_KEY = "tokenizer.json"

# /health/deps values.
HEALTH_OK = "ok"
HEALTH_ERROR = "error: "

# Name of the lakeFS branch every version is committed on.
MAIN_BRANCH = "main"
REPO_PREFIX = "ds-"


class ColumnType(StrEnum):
    """Column types the job writes to schema.json (and as Arrow types in data.parquet)."""

    BOOL = "bool"
    INT64 = "int64"
    FLOAT64 = "float64"
    STRING = "string"


# index.parquet columns for image tasks. `label` is set for image-classification, `boxes` (a list of
# {category, x, y, width, height} in pixels) for object-detection; the other is null. `split` is a Split or null.
INDEX_COLUMNS = ("path", "size", "sha256", "label", "boxes", "split")
# Fields of one entry of `boxes`, in pixels.
BOX_FIELDS = ("category", "x", "y", "width", "height")


def staging_prefix(tenant_id: str, dataset_id: str, import_id: str) -> str:
    return f"{STAGING_ROOT}/{tenant_id}/{dataset_id}/{import_id}/"


def train_prefix(tenant_id: str, project_id: str, run_id: str) -> str:
    return f"{TRAIN_ROOT}/{tenant_id}/{project_id}/{run_id}/"


def prediction_prefix(tenant_id: str, project_id: str, prediction_id: str) -> str:
    return f"{PREDICTIONS_ROOT}/{tenant_id}/{project_id}/{prediction_id}/"


def smoke_prefix(name: str, sha256: str) -> str:
    return f"{JOB_PACKAGE_ROOT}/smoke/{name}-{sha256}/"


def repo_name(dataset_id: str) -> str:
    return f"{REPO_PREFIX}{dataset_id}"
