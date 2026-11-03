"""Task types are the HF slugs, spelled once (core/enums.py); what the platform does with each is the task_types table."""
import re
from pathlib import Path

import pytest

from app.core.enums import TaskType
from app.core.task_catalog import DataKind
from app.core.task_types import IDENTIFIER_PATTERN, TaskTypeBody
from app.integrations.data_plane.mlflow import ModelStage

APP = Path(__file__).parents[2] / "app"
MIGRATIONS = Path(__file__).parents[2] / "migrations"


def test_no_bare_task_type_literal_outside_the_enum():
    slug = re.compile("[\"'](" + "|".join(re.escape(t.value) for t in TaskType) + ")[\"']")
    hits = [str(p) for p in APP.rglob("*.py") if p != APP / "core/enums.py" and slug.search(p.read_text())]
    assert not hits, hits


def _latest_check(constraint: str) -> set[str]:
    """The values of the newest migration that (re)adds `constraint` as `CHECK (col IN (...))`."""
    found = None
    for path in sorted(MIGRATIONS.glob("*.sql")):
        up = path.read_text().split("-- migrate:down")[0]
        for clause in re.findall(rf"ADD CONSTRAINT {constraint} CHECK \(\w+ IN \((.*?)\)\)", up, re.S):
            found = set(re.findall(r"'([^']+)'", clause))
    assert found, constraint
    return found


@pytest.mark.parametrize("constraint", ["projects_task_type_check", "training_runs_task_check"])
def test_the_task_checks_take_the_whole_taxonomy_so_a_new_task_needs_no_migration(constraint):
    assert _latest_check(constraint) == {t.value for t in TaskType}


def test_the_stage_check_mirrors_the_enum():
    assert _latest_check("model_versions_stage_check") == {s.value for s in ModelStage}


TASK_TYPES = next(MIGRATIONS.glob("*_task_types.sql")).read_text()


def test_the_task_types_seed_is_inside_the_taxonomy():
    seeded = set(re.findall(r"^\s*\('([a-z-]+)', '", TASK_TYPES, re.M))
    assert seeded and seeded <= {t.value for t in TaskType}


def test_the_data_kind_check_mirrors_the_enum():
    kinds = re.search(r"data_kind text NOT NULL CHECK \(data_kind IN \(([^)]*)\)", TASK_TYPES).group(1)
    assert set(re.findall(r"'([^']+)'", kinds)) == {k.value for k in DataKind}


def test_the_serving_checks_mirror_the_models():
    sql = next(MIGRATIONS.glob("*_task_types_serving.sql")).read_text()
    assert "encoder ~ '^[A-Za-z_][A-Za-z0-9_]*$'" in sql and IDENTIFIER_PATTERN == r"^[A-Za-z_][A-Za-z0-9_]*$"
    assert "NOT servable OR (encoder IS NOT NULL AND postprocess IS NOT NULL)" in sql
    with pytest.raises(ValueError):
        TaskTypeBody(data_kind=DataKind.IMAGES, servable=True)
