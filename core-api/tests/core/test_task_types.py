"""Task types are the HF slugs, spelled once (core/enums.py); what the platform does with each is the task_types table."""
import re
from pathlib import Path

import pytest

from app.core.enums import TaskType
from app.core.task_catalog import DataKind
from app.core.task_types import IDENTIFIER_PATTERN, TaskTypeBody
from app.integrations.data_plane.mlflow import ModelStage
from tests.support.check_mirror import assert_mirrors, assert_pattern_mirrors

APP = Path(__file__).parents[2] / "app"
MIGRATIONS = Path(__file__).parents[2] / "migrations"


def test_no_bare_task_type_literal_outside_the_enum():
    slug = re.compile("[\"'](" + "|".join(re.escape(t.value) for t in TaskType) + ")[\"']")
    hits = [str(p) for p in APP.rglob("*.py") if p != APP / "core/enums.py" and slug.search(p.read_text())]
    assert not hits, hits


@pytest.mark.parametrize("table, column", [("projects", "task_type"), ("training_runs", "task")])
def test_the_task_checks_take_the_whole_taxonomy_so_a_new_task_needs_no_migration(table, column):
    assert_mirrors(table, column, TaskType)


def test_the_stage_check_mirrors_the_enum():
    assert_mirrors("model_versions", "stage", ModelStage)


TASK_TYPES = next(MIGRATIONS.glob("*_task_types.sql")).read_text()


def test_the_task_types_seed_is_inside_the_taxonomy():
    seeded = set(re.findall(r"^\s*\('([a-z-]+)', '", TASK_TYPES, re.M))
    assert seeded and seeded <= {t.value for t in TaskType}


def test_the_data_kind_check_mirrors_the_enum():
    assert_mirrors("task_types", "data_kind", DataKind)


def test_the_serving_checks_mirror_the_models():
    sql = next(MIGRATIONS.glob("*_task_types_serving.sql")).read_text()
    for column in ("encoder", "postprocess"):
        assert_pattern_mirrors("task_types", column, IDENTIFIER_PATTERN)
    assert "NOT servable OR (encoder IS NOT NULL AND postprocess IS NOT NULL)" in sql
    with pytest.raises(ValueError):
        TaskTypeBody(data_kind=DataKind.IMAGES, servable=True)


def test_every_data_kind_has_a_layout():
    from app.integrations.data_plane.data_kinds import LAYOUTS

    assert set(LAYOUTS) == set(DataKind)
