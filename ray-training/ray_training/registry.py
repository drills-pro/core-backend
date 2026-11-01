"""Which module trains which task. Adding a task = one entry here + a trainer module (+ the `Task` member that mirrors
core-api's TaskType, + the task's presets in core-api's task_catalog); core-api's tests fail when the three disagree.

Entries are "module:function" strings imported on demand: the driver on the CPU pool never loads torch.
"""
import importlib
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ray_training.constants import Task
from ray_training.errors import Unsupported
from ray_training.spec import TrainSpec

if TYPE_CHECKING:
    from ray_training.result import Result

Trainer = Callable[[TrainSpec, object, Path], "Result"]

TRAINERS: Mapping[Task, str] = {
    Task.IMAGE_CLASSIFICATION: "ray_training.classification:train",
    Task.OBJECT_DETECTION: "ray_training.detection:train",
}


def trainer_for(task: Task) -> Trainer:
    target = TRAINERS.get(task)
    if target is None:
        raise Unsupported(f"task {task!r} is not supported")
    module, function = target.split(":")
    return getattr(importlib.import_module(module), function)
