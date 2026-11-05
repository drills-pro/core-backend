"""A trainer is found by the name the spec carries, without importing torch; an unknown one is refused as unsupported."""
import ast
import importlib.util
import inspect

import pytest

from ray_training.constants import FailCode
from ray_training.errors import Unsupported
from ray_training.registry import trainer_for

TRAINER_PARAMS = ["spec", "ds", "work"]  # runner.Trainer: (TrainSpec, dataset, work dir)
TRAINERS = ["ray_training.classification:train", "ray_training.detection:train", "ray_training.text_classification:train"]  # core-api's task_types seed


@pytest.mark.parametrize("target", TRAINERS)
def test_the_name_is_a_function_with_the_trainer_signature_without_importing_it(target):
    module, function = target.split(":")
    tree = ast.parse(open(importlib.util.find_spec(module).origin).read())
    (node,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function]
    assert [a.arg for a in node.args.args] == TRAINER_PARAMS


@pytest.mark.parametrize("target", TRAINERS)
def test_the_name_resolves_to_a_callable_with_the_trainer_signature(target):
    pytest.importorskip("torch")
    train = trainer_for(target)
    assert callable(train) and list(inspect.signature(train).parameters) == TRAINER_PARAMS


@pytest.mark.parametrize("target", ["ray_training.nope:train", "ray_training.classification:nope"])
def test_an_unknown_trainer_is_unsupported(target):
    with pytest.raises(Unsupported) as e:
        trainer_for(target)
    assert e.value.code == FailCode.UNSUPPORTED
