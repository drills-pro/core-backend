"""task_types rows as the database returns them: the ones the migration seeds, read from the migration itself."""
import re
from datetime import UTC, datetime
from pathlib import Path

from app.core.enums import TaskType

_SEED = re.findall(
    r"^\s*\('([a-z-]+)', '(\w+)', (NULL|'[^']+'), (true|false), 'migration'\)",
    next((Path(__file__).parents[2] / "migrations").glob("*_task_types.sql")).read_text(), re.M)

SEEDED = {TaskType(slug): (kind, None if trainer == "NULL" else trainer.strip("'"), servable == "true") for slug, kind, trainer, servable in _SEED}
TRAINABLE = tuple(t for t, (_, trainer, _) in SEEDED.items() if trainer)
SERVABLE = tuple(t for t, (_, _, servable) in SEEDED.items() if servable)


def seeded(task) -> dict | None:
    """The row a lookup of `task` finds in the seeded table."""
    return task_row(task) if task in SEEDED else None


def task_row(task=TaskType.IMAGE_CLASSIFICATION, **over) -> dict:
    kind, trainer, servable = SEEDED[task]
    return {"slug": task, "data_kind": kind, "trainer": trainer, "servable": servable, "enabled": True,
            "updated_by": "migration", "updated_at": datetime.now(UTC), **over}
