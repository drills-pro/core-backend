"""Which function trains a task: the job spec names it (`payload.trainer`, "module:attr", the task bundle's manifest `trainer`),
and the bundle's zip is already on sys.path (a py_module of the job), so a new task is a bundle, not an edit here.

The module is imported on demand: the driver on the CPU pool never loads torch.
"""
import importlib
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ray_core.data import Get
from ray_core.errors import Unsupported
from ray_core.spec import TrainSpec

if TYPE_CHECKING:
    from ray_core.result import Result

Trainer = Callable[[TrainSpec, Path, Get], "Result | None"]  # (spec, work dir, presigned GET); None: not the root rank of a Ray Train run


def load(target: str):
    """The attribute `target` ("module:attr") names, imported from whatever is on sys.path (the bundle)."""
    module, _, attr = target.partition(":")
    try:
        return getattr(importlib.import_module(module), attr)
    except (ImportError, AttributeError) as e:
        raise Unsupported(f"{target!r} is not in a bundle on this job's path") from e
