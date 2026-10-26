"""Task types are the HF slugs, spelled once (core/enums.py); supported is a subset of the taxonomy."""
import re
from pathlib import Path

from app.core.enums import SUPPORTED_TASK_TYPES, TaskType

APP = Path(__file__).parents[2] / "app"


def test_supported_is_a_subset_of_the_taxonomy():
    assert SUPPORTED_TASK_TYPES < set(TaskType)


def test_no_bare_task_type_literal_outside_the_enum():
    slug = re.compile("[\"'](" + "|".join(re.escape(t.value) for t in TaskType) + ")[\"']")
    hits = [str(p) for p in APP.rglob("*.py") if p != APP / "core/enums.py" and slug.search(p.read_text())]
    assert not hits, hits
