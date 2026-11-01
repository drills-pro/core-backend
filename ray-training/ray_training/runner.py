"""The one place a training run becomes a report.json, so no trainer can skip it.

`execute` trains and uploads model.onnx + model.json, and returns the report (never raises). `run` is execute plus
writing report.json, for use without Ray; the Ray driver calls `write_report` itself, in a `finally`.
"""
import json
import math
import tempfile
from pathlib import Path

from ray_training.constants import (
    MODEL_KEY, MODEL_MANIFEST_KEY, REPORT_KEY, REPORT_SCHEMA_VERSION, TRAINER_VERSION, FailCode, Outcome)
from ray_training.data import Get, load_dataset
from ray_training.errors import TrainFailure
from ray_training.registry import Trainer, trainer_for
from ray_training.spec import TrainSpec
from ray_training.store import http_get


def failure_code(e: BaseException) -> FailCode:
    if isinstance(e, TrainFailure):
        return e.code
    if isinstance(e, TimeoutError):
        return FailCode.TIMEOUT
    # torch.OutOfMemoryError subclasses RuntimeError; older torch and CPU paths only carry the message
    if isinstance(e, MemoryError) or type(e).__name__ == "OutOfMemoryError" or "out of memory" in str(e).lower():
        return FailCode.OOM
    return FailCode.INTERNAL


def failed_report(spec: TrainSpec | None, code: FailCode, message: str) -> dict:
    return {**_base(spec), "status": Outcome.FAILED, "code": code, "message": message}


def _base(spec: TrainSpec | None) -> dict:
    return {
        "schema_version": REPORT_SCHEMA_VERSION, "task": spec and spec.task, "preset": spec and spec.preset,
        "dataset": spec and {"version_id": spec.version_id, "lakefs_commit": spec.lakefs_commit},
        "status": Outcome.SUCCEEDED, "code": None, "message": None, "metrics": {}, "history": [], "confusion_matrix": None}


def execute(spec: TrainSpec, store, get: Get = http_get, trainer: Trainer | None = None) -> dict:
    """Train, upload the model, return the report dict. Every exception becomes a failed report."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            result = (trainer or trainer_for(spec.task))(spec, load_dataset(spec, work, get), work)
            manifest = {
                "format": "onnx", "task": spec.task, "preset": spec.preset, "base_model": spec.base_model,
                "base_revision": spec.base_revision, "classes": result.classes, **result.signature,
                "trainer_version": TRAINER_VERSION, "lakefs_commit": spec.lakefs_commit, "metrics": result.metrics}
            if not all(math.isfinite(v) for m in [result.metrics, *result.history] for v in m.values()):
                raise ArithmeticError("a metric is not finite (training diverged)")
            store.put(MODEL_KEY, result.onnx.read_bytes())
            store.put(MODEL_MANIFEST_KEY, json.dumps(manifest).encode())
            for name, path in result.extra_files.items():
                store.put(name, path.read_bytes())
        return {**_base(spec), "metrics": result.metrics, "history": result.history, "confusion_matrix": result.confusion}
    except Exception as e:  # noqa: BLE001 - the report must always be written
        return failed_report(spec, failure_code(e), f"{type(e).__name__}: {e}")


def write_report(store, report: dict) -> None:
    store.put(REPORT_KEY, json.dumps(report, default=str).encode())


def run(spec: TrainSpec, store, get: Get = http_get, trainer: Trainer | None = None) -> dict:
    report = execute(spec, store, get, trainer)
    write_report(store, report)
    return report
