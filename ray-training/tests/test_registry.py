"""Every task has a trainer, found by name without importing torch; an unknown task is refused as unsupported."""
import ast
import importlib.util
import inspect

import pytest

from ray_training.constants import FailCode, Task
from ray_training.errors import Unsupported
from ray_training.registry import TRAINERS, trainer_for

TRAINER_PARAMS = ["spec", "ds", "work"]  # runner.Trainer: (TrainSpec, dataset, work dir)


def test_every_task_has_a_registry_entry():
    assert set(TRAINERS) == set(Task)


@pytest.mark.parametrize("task", list(Task))
def test_the_entry_names_a_function_with_the_trainer_signature_without_importing_it(task):
    module, function = TRAINERS[task].split(":")
    tree = ast.parse(open(importlib.util.find_spec(module).origin).read())
    (node,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function]
    assert [a.arg for a in node.args.args] == TRAINER_PARAMS


@pytest.mark.parametrize("task", list(Task))
def test_the_entry_resolves_to_a_callable_with_the_trainer_signature(task):
    pytest.importorskip("torch")
    train = trainer_for(task)
    assert callable(train) and list(inspect.signature(train).parameters) == TRAINER_PARAMS


def test_an_unknown_task_is_unsupported():
    with pytest.raises(Unsupported) as e:
        trainer_for("image-segmentation")
    assert e.value.code == FailCode.UNSUPPORTED
