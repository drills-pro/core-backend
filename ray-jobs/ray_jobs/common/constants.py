"""Constants the job shares with core-api. The layout and task slugs mirror core-api (tests/ingest_job/test_contract.py asserts equality);
the ones every Ray package needs (SPEC_VERSION, REPORT_KEY, SMOKE_KEY, REPORT_SCHEMA_VERSION, HTTP_TIMEOUT_SECONDS) come from ray_common."""
import re
from http import HTTPStatus

from ray_common.constants import HTTP_TIMEOUT_SECONDS, REPORT_KEY, REPORT_SCHEMA_VERSION, SMOKE_KEY, SPEC_VERSION  # noqa: F401 - shared with the other Ray packages
from ray_common.enums import Split

# --- layout (mirrors app/integrations/data_plane/layout.py; tests/ingest_job asserts equality) ---
UPLOAD_KEY = "upload/source"
OUT_PREFIX = "out/"
OBJECTS_PREFIX = "objects/"
INDEX_KEY = "index.parquet"
DATA_KEY = "data.parquet"
SCHEMA_KEY = "schema.json"
SPLITS = tuple(m.value for m in Split)
MAX_REPORT_ISSUES = 200

# --- task slugs (mirror app.core.enums.TaskType) ---
IMAGE_CLASSIFICATION = "image-classification"
OBJECT_DETECTION = "object-detection"
TABULAR_CLASSIFICATION = "tabular-classification"
TABULAR_REGRESSION = "tabular-regression"
TEXT_CLASSIFICATION = "text-classification"
TEXT_COLUMN = "text"  # a text dataset is a table with these two columns (core-api task_catalog.TEXT_COLUMN, LABEL_COLUMN)
LABEL_COLUMN = "label"  # a tabular target is renamed to this at import, so the trainers read one name (core-api task_catalog.LABEL_COLUMN)
IMAGE_TASKS = (IMAGE_CLASSIFICATION, OBJECT_DETECTION)
TABULAR_TASKS = (TABULAR_CLASSIFICATION, TABULAR_REGRESSION)

UPLOAD_THREADS = 16  # object uploads in flight at once: one blocking PutObject per image is a round trip each
UPLOAD_MAX_PENDING = 32  # uploads queued or running; the next image waits, so memory holds at most this many images
NO_ALARM = 0  # signal.alarm(0) cancels
TIMEOUT_LIMIT = "job_timeout_seconds"  # the Limits field main() reads before Limits.from_dict can fail
HASH_PREFIX_LENGTH = 16  # hex chars of sha256 in Hugging Face object paths
IMAGE_MAGIC = (  # (leading bytes, extension) of the formats in IMAGE_EXTENSIONS that the Hugging Face path accepts
    (b"\xff\xd8\xff", ".jpg"), (b"\x89PNG", ".png"), (b"GIF8", ".gif"), (b"BM", ".bmp"))
RIFF_MAGIC, WEBP_MAGIC = b"RIFF", b"WEBP"
RIFF_FORMAT_OFFSET = 8
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"})
HF_DATA_EXTENSIONS = frozenset({".parquet", ".csv", ".jsonl"})  # never .py: no loading scripts
HF_ORIGIN = "https://huggingface.co/"
HF_ACCESS_STATUSES = frozenset({HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN})  # gated or private: the import sends no token
IGNORED_DIRS = frozenset({"__MACOSX"})
APPLEDOUBLE_PREFIX = "._"  # macOS resource-fork files that some archivers put beside the real file, outside __MACOSX
MIN_NESTED_DEPTH = 3  # <wrapper or split>/<class>/<image>: the depth at which a first folder can be a wrapper or a split
COCO_FILE = "annotations.json"
JSON_EXTENSION = ".json"
SYMLINK_MODE = 0o120000
MODE_MASK = 0o170000
READ_CHUNK = 1 << 20
SPLIT_FROM_FILENAME = {"train": "train", "validation": "validation", "val": "validation", "test": "test"}
BOOL_TOKENS = {"true": True, "false": False}
INT_RE = re.compile(r"^[+-]?\d+$")
WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
