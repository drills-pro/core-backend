"""Standalone ingest job (docs/datasets-plan.md step 7). Runs on Ray, later on core-worker.

Deliberately outside `app/` and free of `app.*` imports: it is shipped to Ray as one file, the way
ray_client.py ships its scripts (base64 into `python -c`), and its contract with core-api is only
  * the staging layout   app/integrations/data_plane/layout.py   (constants mirrored below, drift-tested)
  * the report schema    app/integrations/data_plane/report.schema.json
Stdlib at import time; boto3, pyarrow and PIL are imported lazily inside the I/O edge functions so the pure
logic below is unit-testable without them. In production they come from the Ray image (verified in
core-backend-ray-head: Python 3.11, pyarrow 25.0.1, Pillow 12.3.0, boto3 1.26.76); any Ray image the job runs in
must provide them. core-api's venv has boto3 and pyarrow (the parquet tests run for real) but not PIL.

Entry:  python ingest.py '<params json>'   with STAGING_ENDPOINT / STAGING_ACCESS_KEY / STAGING_SECRET_KEY in
the environment (the job holds no lakeFS credential and writes only under params["prefix"]).
params: {bucket, prefix, source: upload|huggingface, task, target?, split?, limits{...DatasetLimits},
         base?: {index_url|data_url, schema_url?}, hf?: {repo, revision, column?}}

The job writes `out/objects/<path>` for NEW images only plus the merged index: commit_staged imports `out/`
onto main, which keeps the base version's objects already in the repo. Image index `path` is relative to
objects/.

A failed run can leave `out/objects/*` already written in staging, next to a failed report.json and no index. That is
harmless: commit_staged only runs for an `ok` report, and discard / expiry (plan step 8) purge the whole prefix.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import posixpath
import re
import signal
import sys
import tempfile
import unicodedata
import zipfile
from collections import Counter
from dataclasses import dataclass, fields
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

# --- layout (mirrors app/integrations/data_plane/layout.py; tests/ingest_job asserts equality) ---
UPLOAD_KEY = "upload/source"
REPORT_KEY = "report.json"
OUT_PREFIX = "out/"
OBJECTS_PREFIX = "objects/"
INDEX_KEY = "index.parquet"
DATA_KEY = "data.parquet"
SCHEMA_KEY = "schema.json"
SPLITS = ("train", "validation", "test")
REPORT_SCHEMA_VERSION = 1
MAX_REPORT_ISSUES = 200

# --- task slugs (mirror app.core.enums.TaskType) ---
IMAGE_CLASSIFICATION = "image-classification"
OBJECT_DETECTION = "object-detection"
TABULAR_CLASSIFICATION = "tabular-classification"
TABULAR_REGRESSION = "tabular-regression"
IMAGE_TASKS = (IMAGE_CLASSIFICATION, OBJECT_DETECTION)
TABULAR_TASKS = (TABULAR_CLASSIFICATION, TABULAR_REGRESSION)

HTTP_TIMEOUT_SECONDS = 60
NO_ALARM = 0  # signal.alarm(0) cancels
TIMEOUT_LIMIT = "job_timeout_seconds"  # the Limits field main() reads before Limits.from_dict can fail
HASH_PREFIX_LENGTH = 16  # hex chars of sha256 in Hugging Face object paths
IMAGE_MAGIC = (  # (leading bytes, extension) of the formats in IMAGE_EXTENSIONS that the Hugging Face path accepts
    (b"\xff\xd8\xff", ".jpg"), (b"\x89PNG", ".png"), (b"GIF8", ".gif"), (b"BM", ".bmp"))
RIFF_MAGIC, WEBP_MAGIC = b"RIFF", b"WEBP"
RIFF_FORMAT_OFFSET = 8
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"})
HF_DATA_EXTENSIONS = frozenset({".parquet", ".csv", ".jsonl"})  # never .py: no loading scripts
IGNORED_DIRS = frozenset({"__MACOSX"})
COCO_FILE = "annotations.json"
SYMLINK_MODE = 0o120000
MODE_MASK = 0o170000
READ_CHUNK = 1 << 20
SPLIT_FROM_FILENAME = {"train": "train", "validation": "validation", "val": "validation", "test": "test"}
BOOL_TOKENS = {"true": True, "false": False}
INT_RE = re.compile(r"^[+-]?\d+$")
WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


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
    SCHEMA_MISMATCH = "schema_mismatch"
    MERGE_CONFLICT = "merge_conflict"
    DUPLICATE_SKIPPED = "duplicate_skipped"
    HF_CARD_MISMATCH = "hf_card_mismatch"
    HF_SCHEMA_MISMATCH = "hf_schema_mismatch"
    HF_TOO_LARGE = "hf_too_large"
    HF_NO_DATA_FILES = "hf_no_data_files"
    JOB_TIMEOUT = "job_timeout"
    INTERNAL_ERROR = "internal_error"


@dataclass
class Limits:
    """Same names as settings.DatasetLimits; extra keys in the params are ignored."""

    upload_max_bytes: int
    extracted_max_bytes: int
    extracted_max_ratio: int
    max_files: int
    max_archive_depth: int
    max_path_length: int
    image_max_dimension: int
    image_max_bytes: int
    csv_min_rows: int
    csv_max_rows: int
    csv_max_columns: int
    hf_max_repo_bytes: int
    job_timeout_seconds: int

    @classmethod
    def from_dict(cls, d: dict) -> "Limits":
        return cls(**{f.name: d[f.name] for f in fields(cls)})  # KeyError on a missing knob; extra keys ignored


class Report:
    def __init__(self) -> None:
        self.issues: list[dict] = []
        self.omitted = 0
        self.errors = 0

    def add(self, code: Code, severity: Severity, message: str, path: str | None = None) -> None:
        if severity == Severity.ERROR:
            self.errors += 1
        if len(self.issues) >= MAX_REPORT_ISSUES:
            self.omitted += 1
            return
        self.issues.append({"code": code.value, "severity": severity.value, "path": path, "message": message})

    def error(self, code: Code, message: str, path: str | None = None) -> None:
        self.add(code, Severity.ERROR, message, path)

    def warn(self, code: Code, message: str, path: str | None = None) -> None:
        self.add(code, Severity.WARNING, message, path)

    @property
    def failed(self) -> bool:
        return self.errors > 0

    def to_dict(self, stats: dict | None = None, revision: str | None = None, license: str | None = None) -> dict:
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "outcome": Outcome.FAILED if self.failed else Outcome.OK,
            "stats": None if self.failed else stats,
            "issues": self.issues,
            "issues_omitted": self.omitted,
            "revision": revision,
            "license": license,
        }


class LimitExceeded(Exception):
    def __init__(self, code: Code, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# =============================== archive safety ===============================

def is_unsafe_path(name: str) -> bool:
    parts = name.split("/")
    return name.startswith("/") or "\\" in name or bool(WINDOWS_DRIVE_RE.match(name)) or ".." in parts


def is_symlink(info: zipfile.ZipInfo) -> bool:
    return (info.external_attr >> 16) & MODE_MASK == SYMLINK_MODE


def inspect_archive(infos: list[zipfile.ZipInfo], archive_size: int, limits: Limits, report: Report) -> list[zipfile.ZipInfo]:
    """Apply the fixed archive rules; returns the file members worth reading (errors go to the report)."""
    if archive_size > limits.upload_max_bytes:
        report.error(Code.ARCHIVE_TOO_LARGE, f"upload is {archive_size} bytes, limit {limits.upload_max_bytes}")
    members: list[zipfile.ZipInfo] = []
    seen_names: set[str] = set()
    for info in infos:
        name = info.filename
        if is_unsafe_path(name):
            report.error(Code.ARCHIVE_UNSAFE_PATH, "absolute path or '..' in archive member", name)
            continue
        parts = [p for p in name.split("/") if p]
        if IGNORED_DIRS.intersection(parts) or info.is_dir():
            continue
        if is_symlink(info):
            report.error(Code.ARCHIVE_SYMLINK, "symlinks are not allowed", name)
            continue
        if len(name) > limits.max_path_length:
            report.error(Code.PATH_TOO_LONG, f"path longer than {limits.max_path_length}", name)
            continue
        if len(parts) - 1 > limits.max_archive_depth:
            report.error(Code.ARCHIVE_TOO_DEEP, f"deeper than {limits.max_archive_depth} directories", name)
            continue
        if name in seen_names:
            report.error(Code.ARCHIVE_DUPLICATE_PATH, "two archive members share one path", name)
            continue
        seen_names.add(name)
        members.append(info)
    if len(members) > limits.max_files:
        report.error(Code.ARCHIVE_TOO_MANY_FILES, f"{len(members)} files, limit {limits.max_files}")
    total = sum(m.file_size for m in members)
    if total > limits.extracted_max_bytes:
        report.error(Code.ARCHIVE_TOO_LARGE, f"extracted size {total} over limit {limits.extracted_max_bytes}")
    if archive_size and total / archive_size > limits.extracted_max_ratio:
        report.error(Code.ARCHIVE_TOO_LARGE, f"compression ratio over {limits.extracted_max_ratio}: zip bomb")
    return members


class ByteBudget:
    """Counts bytes really decompressed: the sizes in a zip header can lie."""

    def __init__(self, limit: int):
        self.left = limit

    def take(self, n: int, code: Code = Code.ARCHIVE_TOO_LARGE) -> None:
        self.left -= n
        if self.left < 0:
            raise LimitExceeded(code, "data expands past the extracted size limit")

    def read(self, zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
        out = bytearray()
        with zf.open(info) as f:
            while chunk := f.read(READ_CHUNK):
                self.take(len(chunk))
                out += chunk
        return bytes(out)


# ================================== images ====================================

def image_too_large(info: zipfile.ZipInfo, path: str, limits: Limits, report: Report) -> bool:
    """Judge an image member by its declared size before reading it: zipfile never yields more than that, so a
    huge member is refused without being held in memory."""
    if info.file_size <= limits.image_max_bytes:
        return False
    report.error(Code.IMAGE_TOO_LARGE, f"{info.file_size} bytes, limit {limits.image_max_bytes}", path)
    return True


def pil_decode(data: bytes) -> tuple[int, int]:
    from PIL import Image  # lazy: in the Ray image, not in core-api's venv

    with Image.open(io.BytesIO(data)) as im:
        im.verify()
    with Image.open(io.BytesIO(data)) as im:
        return im.size


def check_image(data: bytes, path: str, limits: Limits, report: Report, decode: Callable[[bytes], tuple[int, int]]) -> bool:
    if len(data) > limits.image_max_bytes:
        report.error(Code.IMAGE_TOO_LARGE, f"{len(data)} bytes, limit {limits.image_max_bytes}", path)
        return False
    try:
        width, height = decode(data)  # a real header decode, the extension alone proves nothing
    except TimeoutError:  # the SIGALRM job timeout must not be read as a corrupt image
        raise
    except Exception:
        report.error(Code.CORRUPT_IMAGE, "not a decodable image", path)
        return False
    if max(width, height) > limits.image_max_dimension:
        report.error(Code.IMAGE_TOO_LARGE, f"{width}x{height}, limit {limits.image_max_dimension}", path)
        return False
    return True


def has_image_extension(name: str) -> bool:
    return posixpath.splitext(name)[1].lower() in IMAGE_EXTENSIONS


def is_safe_label(label: str) -> bool:
    """A label is one path segment: no separators, not '.' or '..'."""
    return "/" not in label and "\\" not in label and label not in (".", "..")


def normalise_label(label: str) -> str:
    return unicodedata.normalize("NFC", label).strip()


def case_collisions(labels) -> list[tuple[str, str]]:
    """Pairs of distinct labels that differ only by case."""
    seen: dict[str, str] = {}
    out = []
    for label in sorted(set(labels)):
        key = label.casefold()
        if key in seen:
            out.append((seen[key], label))
        else:
            seen[key] = label
    return out


def check_label_collisions(labels, report: Report) -> None:
    for a, b in case_collisions(labels):
        report.error(Code.LABEL_CASE_COLLISION, f"labels '{a}' and '{b}' differ only by case", b)


def image_row(path: str, data: bytes, label: str | None, boxes: list[dict] | None, split: str | None) -> dict:
    return dict(zip(INDEX_COLUMNS, (path, len(data), hashlib.sha256(data).hexdigest(), label, boxes, split)))


Sink = Callable[[str, bytes], None]


def ingest_classification(zf, members, limits, report, decode, sink: Sink, split: str | None) -> list[dict]:
    """`<class>/<image>`: the first directory is the label."""
    budget = ByteBudget(limits.extracted_max_bytes)
    rows, seen_paths = [], set()
    for info in members:
        parts = [p for p in info.filename.split("/") if p]
        if not has_image_extension(info.filename):
            report.warn(Code.UNSUPPORTED_EXTENSION, "ignored: not an image extension", info.filename)
            continue
        if len(parts) < 2:
            report.error(Code.NO_DATA, "image outside a class folder", info.filename)
            continue
        label = normalise_label(parts[0])
        path = "/".join([label, *parts[1:]])
        if path in seen_paths:  # two members that normalise (NFC, trimmed label) to one object path
            report.error(Code.ARCHIVE_DUPLICATE_PATH, "two archive members map to one object path", path)
            continue
        seen_paths.add(path)
        if image_too_large(info, info.filename, limits, report):
            continue
        data = budget.read(zf, info)
        if check_image(data, info.filename, limits, report, decode):
            sink(path, data)
            rows.append(image_row(path, data, label, None, split))
    check_label_collisions({r[Col.LABEL] for r in rows}, report)
    if not rows and not report.failed:
        report.error(Code.NO_DATA, "no images found")
    return rows


def parse_coco(doc: Any, report: Report) -> list[dict]:
    """Validate a COCO document; returns [{file_name, boxes:[{category,x,y,width,height}]}] (keys: Coco.FILE_NAME, Col.BOXES, Box)."""
    def bad(msg):
        report.error(Code.COCO_INVALID, msg, COCO_FILE)
        return []

    if not isinstance(doc, dict) or not all(isinstance(doc.get(k), list) for k in (Coco.IMAGES, Coco.ANNOTATIONS, Coco.CATEGORIES)):
        return bad("expected an object with images, annotations and categories lists")
    names: dict[Any, str] = {}
    for c in doc[Coco.CATEGORIES]:
        if not isinstance(c, dict) or Coco.ID not in c or not isinstance(c.get(Coco.NAME), str):
            return bad("category needs an id and a name")
        if c[Coco.ID] in names:
            return bad(f"duplicate category id {c[Coco.ID]}")
        names[c[Coco.ID]] = normalise_label(c[Coco.NAME])
    if len(set(names.values())) != len(names):
        return bad("two category ids share one name")
    images: dict[Any, dict] = {}
    for im in doc[Coco.IMAGES]:
        if not isinstance(im, dict) or Coco.ID not in im or not isinstance(im.get(Coco.FILE_NAME), str):
            return bad("image needs an id and a file_name")
        if im[Coco.ID] in images:
            return bad(f"duplicate image id {im[Coco.ID]}")
        images[im[Coco.ID]] = {Coco.FILE_NAME: im[Coco.FILE_NAME], Col.BOXES: []}
    seen_ann = set()
    for a in doc[Coco.ANNOTATIONS]:
        if not isinstance(a, dict) or a.get(Coco.IMAGE_ID) not in images or a.get(Coco.CATEGORY_ID) not in names:
            return bad("annotation references an unknown image or category")
        if Coco.ID in a:
            if a[Coco.ID] in seen_ann:
                return bad(f"duplicate annotation id {a[Coco.ID]}")
            seen_ann.add(a[Coco.ID])
        box = a.get(Coco.BBOX)
        if not (isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box)) or box[2] <= 0 or box[3] <= 0:
            return bad("bbox must be [x, y, width, height] with positive size")
        images[a[Coco.IMAGE_ID]][Col.BOXES].append(dict(zip(BOX_FIELDS, (names[a[Coco.CATEGORY_ID]], *box))))
    return list(images.values())


def ingest_detection(zf, members, limits, report, decode, sink: Sink, split: str | None) -> list[dict]:
    coco = [m for m in members if posixpath.basename(m.filename) == COCO_FILE]
    if len(coco) != 1:
        report.error(Code.COCO_INVALID, f"expected exactly one {COCO_FILE}, found {len(coco)}")
        return []
    budget = ByteBudget(limits.extracted_max_bytes)
    try:
        doc = json.loads(budget.read(zf, coco[0]))
    except ValueError:
        report.error(Code.COCO_INVALID, "not valid JSON", coco[0].filename)
        return []
    entries = parse_coco(doc, report)
    by_name = {m.filename: m for m in members}
    base = posixpath.dirname(coco[0].filename)
    rows, seen_paths = [], set()
    for entry in entries:
        path = posixpath.normpath(posixpath.join(base, entry[Coco.FILE_NAME]))
        info = by_name.get(path)
        if info is None or is_unsafe_path(path):
            report.error(Code.COCO_INVALID, "image listed in annotations is not in the archive", entry[Coco.FILE_NAME])
            continue
        if not has_image_extension(path):
            report.error(Code.UNSUPPORTED_EXTENSION, "not an image extension", path)
            continue
        if path in seen_paths:
            report.error(Code.ARCHIVE_DUPLICATE_PATH, "image listed twice in the annotations", path)
            continue
        seen_paths.add(path)
        if image_too_large(info, path, limits, report):
            continue
        data = budget.read(zf, info)
        if check_image(data, path, limits, report, decode):
            sink(path, data)
            rows.append(image_row(path, data, None, entry[Col.BOXES], split))
    check_label_collisions({b[Box.CATEGORY] for r in rows for b in r[Col.BOXES]}, report)
    if not rows and not report.failed:
        report.error(Code.NO_DATA, "no annotated images found")
    return rows


# ================================== merge =====================================

def row_labels(row: dict) -> list[str]:
    label = row.get(Col.LABEL)
    return [label] if label is not None else [b[Box.CATEGORY] for b in row.get(Col.BOXES) or []]


def merge_index(base_rows: list[dict], new_rows: list[dict], report: Report) -> tuple[list[dict], int]:
    """Strict merge: same path and sha256 is skipped, same path with other bytes is a conflict.
    Categories merge by name; a case-only difference between base and new is a conflict."""
    by_path = {r[Col.PATH]: r for r in base_rows}
    merged, skipped = list(base_rows), 0
    for row in new_rows:
        existing = by_path.get(row[Col.PATH])
        if existing is None:
            by_path[row[Col.PATH]] = row
            merged.append(row)
        elif existing[Col.SHA256] == row[Col.SHA256]:
            skipped += 1
            report.warn(Code.DUPLICATE_SKIPPED, "same path and sha256 as an existing row", row[Col.PATH])
        else:
            report.error(Code.MERGE_CONFLICT, "path exists in the base version with different content", row[Col.PATH])
    check_label_collisions({lab for r in merged for lab in row_labels(r)}, report)
    return merged, skipped


def merge_schema(base: dict | None, new: dict, report: Report) -> bool:
    if base is not None and base != new:
        report.error(Code.SCHEMA_MISMATCH, "columns or types differ from the base version", SCHEMA_KEY)
        return False
    return True


# ================================== tabular ===================================

@dataclass
class Table:
    columns: list[str]
    types: list[ColumnType]
    rows: list[dict]  # typed values, plus Col.SPLIT

    @property
    def schema(self) -> dict:
        return {"columns": [{"name": n, "type": t} for n, t in zip(self.columns, self.types)]}


def infer_type(values: list[str]) -> ColumnType:
    vals = [v for v in values if v != ""]
    if vals and all(v.lower() in BOOL_TOKENS for v in vals):
        return ColumnType.BOOL
    if vals and all(INT_RE.match(v) for v in vals):
        return ColumnType.INT64
    if vals:
        try:
            for v in vals:
                float(v)
            return ColumnType.FLOAT64
        except ValueError:
            pass
    return ColumnType.STRING


def cast(value: str, type_: str) -> Any:
    if value == "":
        return None
    convert = {ColumnType.BOOL: lambda v: BOOL_TOKENS[v.lower()], ColumnType.INT64: int,
               ColumnType.FLOAT64: float, ColumnType.STRING: str}
    return convert[type_](value)


def build_table(header: list[str], rows: list[list[str]], task: str, target: str | None, split: str | None,
                limits: Limits, report: Report) -> Table | None:
    """Header-first string grid -> typed Table, checked against the limits and the task's target rule."""
    header = [h.strip() for h in header]
    if not header or any(h == "" for h in header):
        report.error(Code.CSV_NO_HEADER, "the first row must name every column")
        return None
    dupes = [h for h, n in Counter(header).items() if n > 1]
    if dupes or Col.SPLIT in header:
        report.error(Code.CSV_DUPLICATE_COLUMNS, f"duplicate or reserved column names: {dupes or [Col.SPLIT.value]}")
        return None
    if len(header) > limits.csv_max_columns:
        report.error(Code.CSV_TOO_MANY_COLUMNS, f"{len(header)} columns, limit {limits.csv_max_columns}")
        return None
    if len(rows) > limits.csv_max_rows:
        report.error(Code.CSV_TOO_MANY_ROWS, f"{len(rows)} rows, limit {limits.csv_max_rows}")
        return None
    if len(rows) < limits.csv_min_rows:
        report.error(Code.CSV_TOO_FEW_ROWS, f"{len(rows)} rows, need at least {limits.csv_min_rows}")
        return None
    if target not in header:
        report.error(Code.TARGET_MISSING, f"target column '{target}' not in the header", target)
        return None
    if any(len(r) > len(header) for r in rows):
        report.error(Code.CSV_ROW_TOO_LONG, "a row has more cells than the header has columns")
        return None
    padded = [r + [""] * (len(header) - len(r)) for r in rows]  # shorter rows read as missing cells
    types = [infer_type([r[i] for r in padded]) for i in range(len(header))]
    target_type = types[header.index(target)]
    ok = (
        target_type in (ColumnType.INT64, ColumnType.FLOAT64) if task == TABULAR_REGRESSION else target_type != ColumnType.FLOAT64)
    if not ok:
        report.error(Code.TARGET_TYPE_MISMATCH, f"a {target_type} target does not fit {task}", target)
        return None
    ti = header.index(target)
    if any(r[ti] == "" for r in padded):
        report.error(Code.TARGET_MISSING, "target column has empty cells", target)
        return None
    typed = [{**{h: cast(r[i], types[i]) for i, h in enumerate(header)}, Col.SPLIT: split} for r in padded]
    return Table(header, types, typed)


def read_csv_text(data: bytes) -> tuple[list[str], list[list[str]]]:
    grid = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    return (grid[0], grid[1:]) if grid else ([], [])


def merge_tables(base_schema: dict | None, base_rows: list[dict], table: Table, report: Report) -> list[dict] | None:
    # ponytail: tabular rows are appended, never de-duplicated; add row hashing if repeated rows matter.
    if not merge_schema(base_schema, table.schema, report):
        return None
    return base_rows + table.rows


# ================================== stats =====================================

def compute_stats(rows: list[dict], task: str, target: str | None, skipped: int) -> dict:
    images = task in IMAGE_TASKS
    classes: Counter = Counter()
    for r in rows:
        if images:
            classes.update(set(row_labels(r)))
        elif task == TABULAR_CLASSIFICATION:
            classes[str(r[target])] += 1
    return {
        "file_count": len(rows) if images else 0,
        "total_bytes": sum(r[Col.SIZE] for r in rows) if images else 0,
        "row_count": len(rows),
        "classes": dict(classes),
        "splits": dict(Counter(r[Col.SPLIT] for r in rows if r.get(Col.SPLIT))),
        "duplicates_skipped": skipped,
    }


# ============================== Hugging Face ==================================

HF_CARD_CATEGORY = {t: t for t in IMAGE_TASKS + TABULAR_TASKS}
NUMBER_PREFIXES = ("int", "uint", "float")


def feature_kind(feat: dict) -> FeatureKind:
    """Kind of one card `dataset_info.features` entry."""
    dtype = feat.get(Hf.DTYPE)
    if dtype == Hf.IMAGE:
        return FeatureKind.IMAGE
    if isinstance(dtype, dict) and Hf.CLASS_LABEL in dtype:
        return FeatureKind.CLASS_LABEL
    if isinstance(dtype, str):
        if dtype == ColumnType.STRING:
            return FeatureKind.STRING
        if dtype == ColumnType.BOOL:
            return FeatureKind.BOOL
        if dtype.startswith(NUMBER_PREFIXES):
            return FeatureKind.NUMBER
    seq = feat.get(Hf.SEQUENCE)
    if isinstance(seq, list):
        names = {s.get(Hf.NAME) for s in seq if isinstance(s, dict)}
        if Hf.BBOX in names and names & {Hf.CATEGORY, Hf.CATEGORIES}:
            return FeatureKind.OBJECTS
    return FeatureKind.OTHER


def hf_candidates(task: str, features: list[dict]) -> list[str]:
    kinds = {f.get(Hf.NAME): feature_kind(f) for f in features if isinstance(f, dict)}
    has_image = FeatureKind.IMAGE in kinds.values()
    wanted = {
        IMAGE_CLASSIFICATION: (FeatureKind.CLASS_LABEL,) if has_image else (),
        OBJECT_DETECTION: (FeatureKind.OBJECTS,) if has_image else (),
        TABULAR_CLASSIFICATION: (FeatureKind.CLASS_LABEL, FeatureKind.STRING, FeatureKind.BOOL),
        TABULAR_REGRESSION: (FeatureKind.NUMBER,),
    }[task]
    return [n for n, k in kinds.items() if k in wanted]


def verify_hf(task: str, card_categories: list[str], features: list[dict], column: str | None, report: Report) -> str | None:
    """Both the card's task_categories and the feature schema must agree with the project's task type.
    Returns the chosen target/label column (None on failure)."""
    if HF_CARD_CATEGORY[task] not in (card_categories or []):
        report.error(Code.HF_CARD_MISMATCH, f"card task_categories {card_categories} do not include {task}")
        return None
    candidates = hf_candidates(task, features)
    if column is None and len(candidates) == 1:
        column = candidates[0]
    if column not in candidates:
        report.error(Code.HF_SCHEMA_MISMATCH, f"column {column!r} is not a valid {task} column; candidates: {candidates}")
        return None
    return column


def class_names(features: list[dict], column: str | None) -> list[str] | None:
    for feat in features:
        dtype = feat.get(Hf.DTYPE)
        if feat.get(Hf.NAME) == column and isinstance(dtype, dict):
            names = dtype[Hf.CLASS_LABEL].get(Hf.NAMES)
            if isinstance(names, dict):  # card YAML: {'0': cat, '1': dog}
                return [names[k] for k in sorted(names, key=int)]
            return names
    return None


def split_from_filename(path: str) -> str | None:
    match = re.match(r"^(train|validation|val|test)", posixpath.basename(path))
    return SPLIT_FROM_FILENAME[match.group(1)] if match else None


def select_hf_files(siblings: list[dict], limits: Limits, report: Report) -> list[dict]:
    """Data files only (parquet, csv, jsonl); loading scripts and everything else are never fetched."""
    files = [s for s in siblings if posixpath.splitext(s[Hf.RFILENAME])[1].lower() in HF_DATA_EXTENSIONS]
    if not files:
        report.error(Code.HF_NO_DATA_FILES, "no parquet, csv or jsonl data files in the repository")
    elif any(not isinstance(s.get(Hf.SIZE), int) for s in files):  # an unknown size must not slip under the cap
        report.error(Code.HF_TOO_LARGE, "a data file has no declared size")
    elif sum(s[Hf.SIZE] for s in files) > limits.hf_max_repo_bytes:
        report.error(Code.HF_TOO_LARGE, f"data files exceed {limits.hf_max_repo_bytes} bytes")
    return files


def sniff_extension(data: bytes) -> str | None:
    for magic, ext in IMAGE_MAGIC:
        if data.startswith(magic):
            return ext
    riff_end = RIFF_FORMAT_OFFSET + len(WEBP_MAGIC)
    return ".webp" if data.startswith(RIFF_MAGIC) and data[RIFF_FORMAT_OFFSET:riff_end] == WEBP_MAGIC else None


def hf_label(value: Any, names: list[str] | None) -> str | None:
    """Class id -> name through the card's class_label names; None when the record has no usable label."""
    if value is None:
        return None
    if names and isinstance(value, int):
        label = normalise_label(names[value]) if 0 <= value < len(names) else None
    else:
        label = normalise_label(str(value))
    return label if label and is_safe_label(label) else None  # the label becomes part of an object key


def hf_image_rows(records: list[dict], task: str, column: str, label_names: list[str] | None, split: str | None,
                  limits: Limits, report: Report, decode, sink: Sink, budget: ByteBudget | None = None) -> list[dict]:
    """Records from an HF parquet: {image: {bytes}, <column>: class id | objects{bbox, categories}}."""
    rows, seen_paths = [], set()
    for rec in records:
        data = (rec.get(Hf.IMAGE) or {}).get(Hf.BYTES)
        ext = sniff_extension(data) if data else None
        if ext is None:
            report.error(Code.CORRUPT_IMAGE, "image bytes missing or not a known image format")
            continue
        sha = hashlib.sha256(data).hexdigest()[:HASH_PREFIX_LENGTH]
        if task == IMAGE_CLASSIFICATION:
            label = hf_label(rec.get(column), label_names)
            if label is None:
                report.error(Code.HF_SCHEMA_MISMATCH, f"record has no usable value in column {column!r}", column)
                continue
            path, boxes = f"{label}/{sha}{ext}", None
        else:
            objs = rec.get(column) or {}
            cats = objs.get(Hf.CATEGORIES, objs.get(Hf.CATEGORY, []))
            names = [hf_label(c, label_names) for c in cats]
            if None in names:
                report.error(Code.HF_SCHEMA_MISMATCH, f"record has an unusable category in column {column!r}", column)
                continue
            boxes = [dict(zip(BOX_FIELDS, (n, *b[:4]))) for b, n in zip(objs.get(Hf.BBOX, []), names)]
            label, path = None, f"{sha}{ext}"
        if path in seen_paths:
            report.warn(Code.DUPLICATE_SKIPPED, "same image and label as an earlier record", path)
            continue
        seen_paths.add(path)
        if budget:
            budget.take(len(data))
        if check_image(data, path, limits, report, decode):
            sink(path, data)
            rows.append(image_row(path, data, label, boxes, split))
    check_label_collisions({lab for r in rows for lab in row_labels(r)}, report)
    return rows


# ================================= I/O edge ===================================

class S3Store:
    """Staging bucket under one prefix. boto3 imported lazily."""

    def __init__(self, bucket: str, prefix: str, endpoint: str, access_key: str, secret_key: str):
        import boto3

        self.bucket, self.prefix = bucket, prefix
        self.client = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access_key, aws_secret_access_key=secret_key)

    def size(self, key: str) -> int:
        return self.client.head_object(Bucket=self.bucket, Key=self.prefix + key)["ContentLength"]

    def download(self, key: str, path: Path) -> None:
        self.client.download_file(self.bucket, self.prefix + key, str(path))

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self.prefix + key, Body=data)


def http_get(url: str, max_bytes: int | None = None) -> bytes:
    """GET a presigned or huggingface.co URL; with max_bytes, stop reading one byte past the cap."""
    from urllib.request import urlopen

    with urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310 - presigned or huggingface.co URL built by this job
        data = resp.read(None if max_bytes is None else max_bytes + 1)
    if max_bytes is not None and len(data) > max_bytes:
        raise LimitExceeded(Code.HF_TOO_LARGE, f"a download is over {max_bytes} bytes")
    return data


def _parquet(table) -> bytes:
    import pyarrow.parquet as pq

    sink = io.BytesIO()
    pq.write_table(table, sink)
    return sink.getvalue()


def index_parquet(rows: list[dict]) -> bytes:
    import pyarrow as pa

    box = pa.struct([(f, pa.string() if f == Box.CATEGORY else pa.float64()) for f in BOX_FIELDS])
    types = {Col.SIZE: pa.int64(), Col.BOXES: pa.list_(box)}
    schema = pa.schema([(c.value, types.get(c, pa.string())) for c in INDEX_COLUMNS])
    return _parquet(pa.Table.from_pylist(rows, schema=schema))


def table_parquet(rows: list[dict], table: Table) -> bytes:
    """Explicit Arrow types from the inferred schema: an all-empty column must stay `string`, not Arrow `null`."""
    import pyarrow as pa

    arrow = {ColumnType.BOOL: pa.bool_(), ColumnType.INT64: pa.int64(),
             ColumnType.FLOAT64: pa.float64(), ColumnType.STRING: pa.string()}
    schema = pa.schema([*[(n, arrow[t]) for n, t in zip(table.columns, table.types)], (Col.SPLIT.value, pa.string())])
    columns = [f.name for f in schema]
    return _parquet(pa.Table.from_pylist([{c: r.get(c) for c in columns} for r in rows], schema=schema))


def read_parquet(data: bytes, max_bytes: int | None = None) -> list[dict]:
    """Rows of a parquet file; with max_bytes, refuse one whose footer declares more uncompressed data."""
    import pyarrow.parquet as pq

    if max_bytes is not None:
        meta = pq.ParquetFile(io.BytesIO(data)).metadata
        if sum(meta.row_group(i).total_byte_size for i in range(meta.num_row_groups)) > max_bytes:
            raise LimitExceeded(Code.ARCHIVE_TOO_LARGE, f"parquet expands past {max_bytes} bytes")
    return pq.read_table(io.BytesIO(data)).to_pylist()


def hf_json(repo: str, revision: str) -> dict:
    return json.loads(http_get(f"https://huggingface.co/api/datasets/{repo}/revision/{revision}?blobs=true"))


def hf_records(path: str, data: bytes, budget: ByteBudget) -> list[dict]:
    """Rows of one data file; the budget caps what the file decompresses to (parquet) or holds (csv, jsonl)."""
    ext = posixpath.splitext(path)[1].lower()
    if ext == ".parquet":
        records = read_parquet(data, budget.left)
        budget.take(len(data))
        return records
    budget.take(len(data))
    if ext == ".jsonl":
        return [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]
    header, grid = read_csv_text(data)
    return [dict(zip(header, r)) for r in grid]


def _stringify(v: Any) -> str:
    return "" if v is None else str(v)


def load_base(params: dict, report: Report) -> tuple[Any, dict | None]:
    base = params.get(Param.BASE)
    if not base:
        return None, None
    schema = json.loads(http_get(base[Param.SCHEMA_URL])) if base.get(Param.SCHEMA_URL) else None
    return read_parquet(http_get(base.get(Param.INDEX_URL) or base[Param.DATA_URL])), schema


def run(params: dict, store, decode=pil_decode) -> Report:
    """Do the import and write report.json (always). Returns the Report."""
    report = Report()
    stats = revision = license_ = None
    try:
        limits, task = Limits.from_dict(params[Param.LIMITS]), params[Param.TASK]  # inside the try: bad params still get a report
        split, target = params.get(Param.SPLIT), params.get(Param.TARGET)
        base_rows, base_schema = load_base(params, report)
        skipped = 0
        out_rows: list[dict] | None = None
        sink: Sink = lambda path, data: store.put(OUT_PREFIX + OBJECTS_PREFIX + path, data)
        with tempfile.TemporaryDirectory() as tmp:
            if params[Param.SOURCE] == Source.HUGGINGFACE:
                out_rows, target, table, revision, license_ = _run_hf(params, store, limits, task, report, decode, sink)
            else:
                source = Path(tmp) / "source"
                if (uploaded := store.size(UPLOAD_KEY)) > limits.upload_max_bytes:  # before the download fills the disk
                    raise LimitExceeded(Code.ARCHIVE_TOO_LARGE, f"upload is {uploaded} bytes, limit {limits.upload_max_bytes}")
                store.download(UPLOAD_KEY, source)
                size = source.stat().st_size
                if task in IMAGE_TASKS:
                    out_rows = _run_zip(source, size, task, limits, report, decode, sink, split)
                    table = None
                else:
                    table = _run_csv(source, size, task, target, split, limits, report)
        if not report.failed:
            if task in IMAGE_TASKS:
                out_rows, skipped = merge_index(base_rows or [], out_rows or [], report)
            elif table is not None:
                out_rows = merge_tables(base_schema, base_rows or [], table, report)
        if not report.failed:
            if task in IMAGE_TASKS:
                store.put(OUT_PREFIX + INDEX_KEY, index_parquet(out_rows))
            else:
                store.put(OUT_PREFIX + DATA_KEY, table_parquet(out_rows, table))
                store.put(OUT_PREFIX + SCHEMA_KEY, json.dumps(table.schema).encode())
            stats = compute_stats(out_rows, task, target, skipped)
    except LimitExceeded as e:
        report.error(e.code, e.message)
    except TimeoutError:
        report.error(Code.JOB_TIMEOUT, "job exceeded its time limit")
    except Exception as e:  # noqa: BLE001 - the report must always be written
        report.error(Code.INTERNAL_ERROR, f"{type(e).__name__}: {e}")
    finally:
        signal.alarm(NO_ALARM)  # the job timeout must not fire inside the report write below
    store.put(REPORT_KEY, json.dumps(report.to_dict(stats, revision, license_)).encode())
    return report


def _run_zip(source, size, task, limits, report, decode, sink, split):
    try:
        zf = zipfile.ZipFile(source)
    except zipfile.BadZipFile:
        report.error(Code.NO_DATA, "upload is not a valid zip archive")
        return []
    with zf:
        members = inspect_archive(zf.infolist(), size, limits, report)
        if report.failed:
            return []
        ingest = ingest_classification if task == IMAGE_CLASSIFICATION else ingest_detection
        return ingest(zf, members, limits, report, decode, sink, split)


def _run_csv(source, size, task, target, split, limits, report):
    if size > limits.upload_max_bytes:
        report.error(Code.ARCHIVE_TOO_LARGE, f"upload is {size} bytes, limit {limits.upload_max_bytes}")
        return None
    try:
        header, grid = read_csv_text(source.read_bytes())
    except UnicodeDecodeError:
        report.error(Code.CSV_NO_HEADER, "file is not valid UTF-8 text")
        return None
    return build_table(header, grid, task, target, split, limits, report)


def _run_hf(params, store, limits, task, report, decode, sink):
    hf = params[Param.HF]
    info = hf_json(hf[Param.REPO], hf[Param.REVISION])  # the job pins the commit sha the API answers with, and downloads at that sha
    card = info.get(Hf.CARD_DATA) or {}
    features = ((card.get(Hf.DATASET_INFO) or {}).get(Hf.FEATURES)) or []
    column = verify_hf(task, card.get(Hf.TASK_CATEGORIES) or [], features, hf.get(Param.COLUMN), report)
    files = select_hf_files(info.get(Hf.SIBLINGS, []), limits, report)
    revision, license_ = info.get(Hf.SHA, hf[Param.REVISION]), card.get(Hf.LICENSE)
    if report.failed:
        return None, column, None, revision, license_
    names = class_names(features, column)
    budget = ByteBudget(limits.extracted_max_bytes)  # decompressed data across all files, however small the declared sizes
    records_by_split = []
    for f in files:
        url = f"https://huggingface.co/datasets/{hf[Param.REPO]}/resolve/{revision}/{f[Hf.RFILENAME]}"
        data = http_get(url, limits.hf_max_repo_bytes)
        records_by_split.append((split_from_filename(f[Hf.RFILENAME]) or params.get(Param.SPLIT), hf_records(f[Hf.RFILENAME], data, budget)))
    if task in IMAGE_TASKS:
        rows = [r for sp, recs in records_by_split
                for r in hf_image_rows(recs, task, column, names, sp, limits, report, decode, sink, budget)]
        return rows, column, None, revision, license_
    header = [f[Hf.NAME] for f in features if f.get(Hf.NAME)]
    grid, splits = [], []
    for sp, recs in records_by_split:
        grid += [[_stringify(rec.get(h)) for h in header] for rec in recs]
        splits += [sp] * len(recs)
    table = build_table(header, grid, task, column, None, limits, report)
    if table is not None:
        for row, sp in zip(table.rows, splits):
            row[Col.SPLIT] = sp
    return None, column, table, revision, license_


def main(argv: list[str]) -> int:
    import os

    params = json.loads(argv[1])
    store = S3Store(params[Param.BUCKET], params[Param.PREFIX], os.environ["STAGING_ENDPOINT"],
                    os.environ["STAGING_ACCESS_KEY"], os.environ["STAGING_SECRET_KEY"])

    def on_alarm(*_):
        raise TimeoutError

    signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(params.get(Param.LIMITS, {}).get(TIMEOUT_LIMIT, NO_ALARM))  # run() reports bad limits
    try:
        return 1 if run(params, store).failed else 0
    finally:
        signal.alarm(NO_ALARM)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
