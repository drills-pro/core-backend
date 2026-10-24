"""Merging a new import into the base version (strict: same path and sha256 is skipped, other bytes conflict)."""
import json
from typing import Any

from ray_jobs.common.constants import SCHEMA_KEY
from ray_jobs.common.enums import Code, Col, Param
from ray_jobs.common.labels import check_label_collisions
from ray_jobs.common.parquet import read_parquet
from ray_jobs.common.report import Report
from ray_jobs.common.rows import row_labels
from ray_jobs.common.store import http_get
from ray_jobs.common.table import Table


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


def merge_tables(base_schema: dict | None, base_rows: list[dict], table: Table, report: Report) -> list[dict] | None:
    # ponytail: tabular rows are appended, never de-duplicated; add row hashing if repeated rows matter.
    if not merge_schema(base_schema, table.schema, report):
        return None
    return base_rows + table.rows


def load_base(params: dict, report: Report) -> tuple[Any, dict | None]:
    base = params.get(Param.BASE)
    if not base:
        return None, None
    schema = json.loads(http_get(base[Param.SCHEMA_URL])) if base.get(Param.SCHEMA_URL) else None
    return read_parquet(http_get(base.get(Param.INDEX_URL) or base[Param.DATA_URL])), schema
