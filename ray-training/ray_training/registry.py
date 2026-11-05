"""Which function trains a task: the job spec names it (`payload.trainer`, "module:function", the task_types row's trainer in
core-api), so a new task needs a trainer module in this package and a row, not an edit here.

The module is imported on demand: the driver on the CPU pool never loads torch.
"""
import importlib
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ray_training.errors import Unsupported
from ray_training.spec import TrainSpec

if TYPE_CHECKING:
    from ray_training.result import Result

Trainer = Callable[[TrainSpec, object, Path], "Result | None"]  # None: not the root rank of a Ray Train run


def trainer_for(target: str) -> Trainer:
    module, _, function = target.partition(":")
    try:
        return getattr(importlib.import_module(module), function)
    except (ImportError, AttributeError) as e:
        raise Unsupported(f"trainer {target!r} is not in this package") from e
