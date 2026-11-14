"""Failures a trainer raises on purpose; anything else becomes `internal` (or `oom`) in the runner."""
from ray_core.constants import FailCode


class TrainFailure(Exception):
    code = FailCode.INTERNAL


class BadData(TrainFailure):
    code = FailCode.BAD_DATA


class Unsupported(TrainFailure):
    code = FailCode.UNSUPPORTED
