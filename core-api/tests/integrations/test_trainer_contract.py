"""The contract between the catalog and the trainer package (top-level ray-training/): the trainer mirrors the task and
preset enums and keeps a registry of its trainers instead of importing core-api, so these tests fail when they drift.
A new trainable task needs a `task_catalog` entry, a `Task` member and a `registry.TRAINERS` entry, in that order of failing."""
import uuid

import pytest

from app.core import task_catalog
from app.core.task_catalog import TRAINABLE, Resample, TextPreprocessing, is_table
from app.integrations.job_spec import Resources, TrainData, TrainPayload, TrainSpec
from ray_training import constants as trainer
from ray_training.registry import TRAINERS
from ray_training.spec import parse_spec
from tests.support.presets import IMAGENET, preset



def test_the_trainer_runs_exactly_the_trainable_tasks():
    assert {t.value for t in trainer.Task} == {t.value for t in TRAINABLE}


def test_every_trainable_task_has_a_trainer_registry_entry():
    assert {t.value for t in TRAINERS} == {t.value for t in TRAINABLE}


def test_the_trainer_mirrors_the_resample_enum():
    assert {r.value: r.name for r in trainer.Resample} == {r.value: r.name for r in Resample}


def spec_of(task, preset) -> TrainSpec:
    info = preset
    data = TrainData(version_id=uuid.uuid4(), lakefs_commit="c0ffee", **(
        {"data_url": "http://d"} if is_table(task) else {"index_url": "http://i", "manifest_url": "http://m"}))
    payload = TrainPayload(
        bucket="b", prefix="p/", task=task, preset=info.name, base_model=info.base_model, learning_rate=info.learning_rate,
        preprocessing=info.preprocessing, data=data, hyperparameters={})
    return TrainSpec(job_id="j", tenant_id=uuid.uuid4(), payload=payload, resources=Resources(cpu=1, memory_bytes=1, gpu=1))


def preset_of(task):
    text = task_catalog.info(task).data_kind is task_catalog.DataKind.TEXT
    return preset(task, preprocessing={"max_length": 256} if text else IMAGENET)


@pytest.mark.parametrize("task", TRAINABLE)
def test_the_trainer_reads_what_a_preset_says(task):
    info = preset_of(task)
    spec = parse_spec(spec_of(task, info).model_dump_json())
    assert (spec.task.value, spec.preset) == (task.value, info.name)
    assert (spec.base_model, spec.base_revision, spec.learning_rate) == (info.base_model.repo_id, info.base_model.revision, info.learning_rate)
    pre = spec.preprocessing
    if isinstance(info.preprocessing, TextPreprocessing):
        assert pre.max_length == info.preprocessing.max_length and spec.data_url == "http://d"
        return
    assert (pre.mean, pre.std, pre.rescale, pre.resample.value) == (
        info.preprocessing.mean, info.preprocessing.std, info.preprocessing.rescale, info.preprocessing.resample.value)
