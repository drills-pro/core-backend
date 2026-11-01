"""The contract between the catalog and the trainer package (top-level ray-training/): the trainer mirrors the task and
preset enums and keeps a registry of its trainers instead of importing core-api, so these tests fail when they drift.
A new trainable task needs a `task_catalog` entry, a `Task` member and a `registry.TRAINERS` entry, in that order of failing."""
import uuid

import pytest

from app.core.enums import TrainPreset
from app.core.task_catalog import TASKS, TRAINABLE, Resample
from app.integrations.job_spec import Resources, TrainData, TrainPayload, TrainSpec
from ray_training import constants as trainer
from ray_training.registry import TRAINERS
from ray_training.spec import parse_spec

CASES = [(task, preset) for task in TRAINABLE for preset in TrainPreset]


def test_the_trainer_runs_exactly_the_trainable_tasks():
    assert {t.value for t in trainer.Task} == {t.value for t in TRAINABLE}


def test_every_trainable_task_has_a_trainer_registry_entry():
    assert {t.value for t in TRAINERS} == {t.value for t in TRAINABLE}


def test_the_trainer_mirrors_the_preset_and_resample_enums():
    assert {p.value for p in trainer.Preset} == {p.value for p in TrainPreset}
    assert {r.value: r.name for r in trainer.Resample} == {r.value: r.name for r in Resample}


def spec_of(task, preset) -> TrainSpec:
    info = TASKS[task].presets[preset]
    data = TrainData(index_url="http://i", manifest_url="http://m", version_id=uuid.uuid4(), lakefs_commit="c0ffee")
    payload = TrainPayload(
        bucket="b", prefix="p/", task=task, preset=preset, base_model=info.base_model, learning_rate=info.learning_rate,
        preprocessing=info.preprocessing, data=data, hyperparameters={})
    return TrainSpec(job_id="j", tenant_id=uuid.uuid4(), payload=payload, resources=Resources(cpu=1, memory_bytes=1, gpu=1))


@pytest.mark.parametrize("task,preset", CASES)
def test_the_trainer_reads_what_the_catalog_says_for_every_preset(task, preset):
    info = TASKS[task].presets[preset]
    spec = parse_spec(spec_of(task, preset).model_dump_json())
    assert (spec.task.value, spec.preset.value) == (task.value, preset.value)
    assert (spec.base_model, spec.base_revision, spec.learning_rate) == (info.base_model.repo_id, info.base_model.revision, info.learning_rate)
    pre = spec.preprocessing
    assert (pre.mean, pre.std, pre.rescale, pre.resample.value) == (
        info.preprocessing.mean, info.preprocessing.std, info.preprocessing.rescale, info.preprocessing.resample.value)
