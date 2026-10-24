"""CSV upload: header, type inference, and the task's target rule."""
import csv
import io
from collections import Counter
from pathlib import Path
from typing import Any

from ray_jobs.common.constants import BOOL_TOKENS, INT_RE, TABULAR_REGRESSION
from ray_jobs.common.enums import Code, Col, ColumnType
from ray_jobs.common.limits import Limits
from ray_jobs.common.report import Report
from ray_jobs.common.table import Table


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


def read_csv_upload(source: Path, task: str, target: str | None, split: str | None, limits: Limits, report: Report) -> Table | None:
    size = source.stat().st_size
    if size > limits.upload_max_bytes:
        report.error(Code.ARCHIVE_TOO_LARGE, f"upload is {size} bytes, limit {limits.upload_max_bytes}")
        return None
    try:
        header, grid = read_csv_text(source.read_bytes())
    except UnicodeDecodeError:
        report.error(Code.CSV_NO_HEADER, "file is not valid UTF-8 text")
        return None
    return build_table(header, grid, task, target, split, limits, report)
