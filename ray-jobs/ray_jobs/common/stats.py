"""Stats block of report.json."""
from collections import Counter

from ray_jobs.common.constants import (
    IMAGE_CLASSIFICATION, IMAGE_TASKS, OBJECT_DETECTION, TABULAR_CLASSIFICATION, TABULAR_REGRESSION, TEXT_CLASSIFICATION)
from ray_jobs.common.enums import Col
from ray_jobs.common.rows import row_labels


def _image_classes(rows: list[dict], target: str | None) -> Counter:
    return Counter(label for r in rows for label in set(row_labels(r)))


def _target_classes(rows: list[dict], target: str | None) -> Counter:
    return Counter(str(r[target]) for r in rows)


def _no_classes(rows: list[dict], target: str | None) -> Counter:
    return Counter()


CLASS_COUNTERS = {
    IMAGE_CLASSIFICATION: _image_classes, OBJECT_DETECTION: _image_classes,
    TABULAR_CLASSIFICATION: _target_classes, TEXT_CLASSIFICATION: _target_classes,
    TABULAR_REGRESSION: _no_classes,
}  # a task missing here raises KeyError rather than reporting no classes


def compute_stats(rows: list[dict], task: str, target: str | None, skipped: int) -> dict:
    images = task in IMAGE_TASKS
    classes = CLASS_COUNTERS[task](rows, target)
    return {
        "file_count": len(rows) if images else 0,
        "total_bytes": sum(r[Col.SIZE] for r in rows) if images else 0,
        "row_count": len(rows),
        "classes": dict(classes),
        "splits": dict(Counter(r[Col.SPLIT] for r in rows if r.get(Col.SPLIT))),
        "duplicates_skipped": skipped,
    }
