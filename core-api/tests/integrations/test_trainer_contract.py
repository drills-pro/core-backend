"""The contract between the task_types rows and the trainer package (top-level ray-training/): the spec names the trainer
function of a task, and the package must hold it, so these tests fail when the two drift. A new trainable task needs a
trainer module in the package and a task_types row naming it."""
import ast
import importlib.util
import uuid

import pytest

from app.core.enums import TaskType
from app.core.task_catalog import DataKind, Resample, TextPreprocessing
from app.integrations.job_spec import Resources, TrainData, TrainPayload, TrainSpec
from ray_training import constants as trainer
from ray_training.spec import parse_spec
from tests.support.presets import IMAGENET, preset
from tests.support.task_types import SEEDED, TRAINABLE



TRAINER_PARAMS = ["spec", "ds", "work"]


def test_the_trainer_package_knows_only_task_types():
    assert {t.value for t in trainer.Task} <= {t.value for t in TaskType}


@pytest.mark.parametrize("task", TRAINABLE)
def test_every_seeded_trainer_is_a_function_with_the_trainer_signature(task):
    module, function = SEEDED[task][1].split(":")
    tree = ast.parse(open(importlib.util.find_spec(module).origin).read())
    (node,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function]
    assert [a.arg for a in node.args.args] == TRAINER_PARAMS


def test_the_trainer_mirrors_the_resample_enum():
    assert {r.value: r.name for r in trainer.Resample} == {r.value: r.name for r in Resample}


def spec_of(task, preset) -> TrainSpec:
    info = preset
    kind, trainer_name, _ = SEEDED[task]
    data = TrainData(version_id=uuid.uuid4(), lakefs_commit="c0ffee", **(
        {"data_url": "http://d"} if kind != DataKind.IMAGES else {"index_url": "http://i", "manifest_url": "http://m"}))
    payload = TrainPayload(
        bucket="b", prefix="p/", task=task, trainer=trainer_name, data_kind=kind, preset=info.name, base_model=info.base_model, learning_rate=info.learning_rate,
        preprocessing=info.preprocessing, data=data, hyperparameters={})
    return TrainSpec(job_id="j", tenant_id=uuid.uuid4(), payload=payload, resources=Resources(cpu=1, memory_bytes=1, gpu=1))


def preset_of(task):
    text = SEEDED[task][0] == DataKind.TEXT
    return preset(task, preprocessing={"max_length": 256} if text else IMAGENET)


@pytest.mark.parametrize("task", TRAINABLE)
def test_the_trainer_reads_what_a_preset_says(task):
    info = preset_of(task)
    spec = parse_spec(spec_of(task, info).model_dump_json())
    assert (spec.task, spec.preset) == (task.value, info.name)
    assert (spec.base_model, spec.base_revision, spec.learning_rate) == (info.base_model.repo_id, info.base_model.revision, info.learning_rate)
    pre = spec.preprocessing
    if isinstance(info.preprocessing, TextPreprocessing):
        assert pre.max_length == info.preprocessing.max_length and spec.data_url == "http://d"
        return
    assert (pre.mean, pre.std, pre.rescale, pre.resample.value) == (
        info.preprocessing.mean, info.preprocessing.std, info.preprocessing.rescale, info.preprocessing.resample.value)
