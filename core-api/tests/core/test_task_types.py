"""Task types are the HF slugs, spelled once (core/enums.py); what the platform does with each is core/task_catalog.py."""
import re
from pathlib import Path
from typing import get_args

import pytest

from app.core import task_catalog
from app.core.enums import TaskType, TrainPreset
from app.core.task_catalog import TASKS
from app.integrations.data_plane.mlflow import ModelStage
from app.integrations.job_spec import TrainTask

APP = Path(__file__).parents[2] / "app"
MIGRATIONS = Path(__file__).parents[2] / "migrations"


def test_supported_is_a_subset_of_the_taxonomy():
    assert set(TASKS) < set(TaskType)


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


def test_the_train_job_spec_accepts_exactly_the_trainable_tasks():
    assert set(get_args(TrainTask)) == {t for t in TASKS if task_catalog.trainable(t)}


def test_every_trainable_task_has_a_pinned_base_model_per_preset():
    for task in (t for t in TASKS if task_catalog.trainable(t)):
        assert set(TASKS[task].presets) == set(TrainPreset), task
        assert all(len(m.base_model.revision) == 40 for m in TASKS[task].presets.values()), task  # pinned to a commit, never "main"
