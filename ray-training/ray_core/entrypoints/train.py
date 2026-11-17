"""Driver of a training job: starts a Ray Train run (a TorchTrainer) and writes report.json from what it returns.

    python -m ray_core.entrypoints.train '<job spec json>'      # STAGING_* credentials in the environment

Submit it with entrypointNumGpus=0: the GPUs belong to the Train workers, not to the driver. The driver imports torch
(ray.train.torch does), so it runs where the image has it. Exit code 1 when the report is failed.
"""
import json
import math
import os
import sys
import threading

import ray
from ray.train import CheckpointConfig, FailureConfig, RunConfig, ScalingConfig
from ray.train.torch import TorchTrainer

from ray_core.constants import LOCAL_GPUS_ENV, TRAIN_ATTEMPTS, TRAIN_STATE_KEEP, FailCode, Outcome
from ray_core.errors import Unsupported
from ray_core.runner import failed_report, failure_code, write_report
from ray_core.spec import TrainSpec, parse_spec
from ray_core.store import store_from_env, train_state_storage
from ray_core.worker import REPORT_FIELD, train_loop


def scaling(spec: TrainSpec) -> ScalingConfig:
    """One Train worker per GPU (the preset's `gpu`), or one worker on a share of a GPU, or one CPU worker for 0. The
    resources are per worker. TRAIN_NUM_GPUS overrides the preset (CI sets 0)."""
    gpus = float(os.environ.get(LOCAL_GPUS_ENV, spec.gpu))
    per_worker = {"CPU": spec.cpu, "memory": spec.memory_bytes}
    if gpus == 0:
        return ScalingConfig(num_workers=1, use_gpu=False, resources_per_worker=per_worker)
    if gpus < 1:
        return ScalingConfig(num_workers=1, use_gpu=True, resources_per_worker={**per_worker, "GPU": gpus})
    if gpus != math.floor(gpus):
        raise Unsupported(f"gpu must be a whole number or a fraction of one, got {gpus}")
    return ScalingConfig(num_workers=int(gpus), use_gpu=True, resources_per_worker={**per_worker, "GPU": 1})


def trainer_for_spec(spec: TrainSpec, spec_text: str) -> TorchTrainer:
    filesystem, path = train_state_storage(spec.bucket, spec.job_id)
    return TorchTrainer(
        train_loop, train_loop_config={"spec": spec_text}, scaling_config=scaling(spec),
        run_config=RunConfig(
            name=spec.job_id, storage_path=path, storage_filesystem=filesystem,
            failure_config=FailureConfig(max_failures=TRAIN_ATTEMPTS),
            checkpoint_config=CheckpointConfig(num_to_keep=TRAIN_STATE_KEEP)))


def fit(spec: TrainSpec, spec_text: str) -> dict:
    """Run the trainer for at most spec.max_runtime_s and return the report. Raises on a failed attempt, TimeoutError on the limit."""
    outcome: dict = {}

    def run():
        try:
            outcome["result"] = trainer_for_spec(spec, spec_text).fit()
        except BaseException as e:  # noqa: BLE001 - handed to the main thread
            outcome["error"] = e

    thread = threading.Thread(target=run, daemon=True)  # daemon: a timed-out run must not keep the process alive
    thread.start()
    thread.join(spec.max_runtime_s or None)
    if thread.is_alive():
        raise TimeoutError("job exceeded its time limit")
    if "error" in outcome:
        raise outcome["error"]
    return json.loads(outcome["result"].metrics[REPORT_FIELD])


def cause(error: BaseException) -> BaseException:
    """The exception a worker raised, from the error Ray Train wraps it in (a TrainFailure keeps its failure code)."""
    for worker_error in getattr(error, "worker_failures", {}).values():
        original = getattr(worker_error, "exc", None) or worker_error
        if isinstance(original, BaseException):
            return original
    return error


def main(argv: list[str]) -> int:
    payload = json.loads(argv[1]).get("payload", {})
    store = store_from_env(payload.get("bucket", ""), payload.get("prefix", ""))
    report, spec = failed_report(None, FailCode.INTERNAL, "driver ended before a report"), None
    try:
        try:
            spec = parse_spec(argv[1])
        except (ValueError, KeyError) as e:  # unknown version, kind, task or preset; or a missing field
            report = failed_report(None, FailCode.UNSUPPORTED, f"{type(e).__name__}: {e}")
            return 1
        ray.init(address="auto")
        report = fit(spec, argv[1])
    except BaseException as e:  # noqa: BLE001 - the report must always be written
        original = cause(e)
        report = failed_report(spec, failure_code(original), f"{type(original).__name__}: {original}")
    finally:
        write_report(store, report)
    return 1 if report["status"] == Outcome.FAILED else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
