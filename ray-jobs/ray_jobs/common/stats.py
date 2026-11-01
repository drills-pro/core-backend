"""Stats block of report.json."""
from collections import Counter

from ray_jobs.common.constants import IMAGE_TASKS, TABULAR_CLASSIFICATION, TEXT_CLASSIFICATION
from ray_jobs.common.enums import Col
from ray_jobs.common.rows import row_labels


def compute_stats(rows: list[dict], task: str, target: str | None, skipped: int) -> dict:
    images = task in IMAGE_TASKS
    classes: Counter = Counter()
    for r in rows:
        if images:
            classes.update(set(row_labels(r)))
        elif task in (TABULAR_CLASSIFICATION, TEXT_CLASSIFICATION):
            classes[str(r[target])] += 1
    return {
        "file_count": len(rows) if images else 0,
        "total_bytes": sum(r[Col.SIZE] for r in rows) if images else 0,
        "row_count": len(rows),
        "classes": dict(classes),
        "splits": dict(Counter(r[Col.SPLIT] for r in rows if r.get(Col.SPLIT))),
        "duplicates_skipped": skipped,
    }
