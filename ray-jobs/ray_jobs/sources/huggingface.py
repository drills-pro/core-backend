"""Hugging Face source: card and schema checks, data file selection, records to rows."""
import hashlib
import json
import posixpath
import re
from typing import Any

from ray_jobs.common.constants import (
    HASH_PREFIX_LENGTH, HF_DATA_EXTENSIONS, IMAGE_CLASSIFICATION, IMAGE_MAGIC, IMAGE_TASKS, OBJECT_DETECTION,
    RIFF_FORMAT_OFFSET, RIFF_MAGIC, SPLIT_FROM_FILENAME, TABULAR_CLASSIFICATION, TABULAR_REGRESSION, TABULAR_TASKS, WEBP_MAGIC)
from ray_jobs.common.enums import BOX_FIELDS, Code, Col, ColumnType, FeatureKind, Hf, Param
from ray_jobs.common.labels import check_label_collisions, is_safe_label, normalise_label
from ray_jobs.common.job import Job
from ray_jobs.common.limits import Limits
from ray_jobs.common.parquet import read_parquet
from ray_jobs.common.report import Report
from ray_jobs.common.rows import image_row, row_labels
from ray_jobs.common.store import Sink, http_get
from ray_jobs.common.table import Extracted
from ray_jobs.sources.archive import ByteBudget
from ray_jobs.sources.csv_table import build_table, read_csv_text
from ray_jobs.sources.images import check_image


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


def extract(job: Job) -> Extracted:
    """The Hugging Face import: rows (image tasks) or a table (tabular tasks), the commit sha and the card's license."""
    params, limits, task, report, decode, sink = job.params, job.limits, job.task, job.report, job.decode, job.sink
    hf = params[Param.HF]
    info = hf_json(hf[Param.REPO], hf[Param.REVISION])  # the job pins the commit sha the API answers with, and downloads at that sha
    card = info.get(Hf.CARD_DATA) or {}
    features = ((card.get(Hf.DATASET_INFO) or {}).get(Hf.FEATURES)) or []
    column = verify_hf(task, card.get(Hf.TASK_CATEGORIES) or [], features, hf.get(Param.COLUMN), report)
    files = select_hf_files(info.get(Hf.SIBLINGS, []), limits, report)
    revision, license_ = info.get(Hf.SHA, hf[Param.REVISION]), card.get(Hf.LICENSE)
    if report.failed:
        return Extracted(None, column, None, revision, license_)
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
        return Extracted(rows, column, None, revision, license_)
    header = [f[Hf.NAME] for f in features if f.get(Hf.NAME)]
    grid, splits = [], []
    for sp, recs in records_by_split:
        grid += [[_stringify(rec.get(h)) for h in header] for rec in recs]
        splits += [sp] * len(recs)
    table = build_table(header, grid, task, column, None, limits, report)
    if table is not None:
        for row, sp in zip(table.rows, splits):
            row[Col.SPLIT] = sp
    return Extracted(None, column, table, revision, license_)
