"""A trainer is found by the name the spec carries, from whatever bundle is on the path, without importing torch; an unknown
one is refused as unsupported."""
import ast
import importlib.util
import inspect

import pytest

from ray_core.constants import FailCode
from ray_core.errors import Unsupported
from ray_core.registry import load
from tests.tiny import TRAINER

TRAINER_PARAMS = ["spec", "work", "get"]  # registry.Trainer: (TrainSpec, work dir, presigned GET)


def test_the_name_is_a_function_with_the_trainer_signature_without_importing_it():
    module, function = TRAINER.split(":")
    tree = ast.parse(open(importlib.util.find_spec(module).origin).read())
    (node,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function]
    assert [a.arg for a in node.args.args] == TRAINER_PARAMS


def test_the_name_resolves_to_a_callable_with_the_trainer_signature():
    pytest.importorskip("torch")
    train = load(TRAINER)
    assert callable(train) and list(inspect.signature(train).parameters) == TRAINER_PARAMS


@pytest.mark.parametrize("target", ["tests.nope:train", "tests.tiny:nope"])
def test_an_unknown_trainer_is_unsupported(target):
    with pytest.raises(Unsupported) as e:
        load(target)
    assert e.value.code == FailCode.UNSUPPORTED
