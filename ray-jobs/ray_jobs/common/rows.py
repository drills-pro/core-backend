"""One index.parquet row."""
import hashlib

from ray_jobs.common.enums import INDEX_COLUMNS, Box, Col


def image_row(path: str, data: bytes, label: str | None, boxes: list[dict] | None, split: str | None) -> dict:
    return dict(zip(INDEX_COLUMNS, (path, len(data), hashlib.sha256(data).hexdigest(), label, boxes, split)))


def row_labels(row: dict) -> list[str]:
    label = row.get(Col.LABEL)
    return [label] if label is not None else [b[Box.CATEGORY] for b in row.get(Col.BOXES) or []]
