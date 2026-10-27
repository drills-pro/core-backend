"""Driver of a training job (plan "Ray execution"): runs on the CPU pool, trains in a Ray task on the GPU worker.

    python -m ray_training.entrypoints.train '<job spec json>'      # STAGING_* credentials in the environment

Submit it with entrypointNumGpus=0 and entrypointResources={"cpu_pool": 1}: if the driver reserved the only GPU, the
task could never schedule. Exit code 1 when the report is failed.
"""
import json
import os
import signal
import sys

import ray

from ray_training.constants import DEFAULT_TASK_GPUS, LOCAL_GPUS_ENV, NO_ALARM, FailCode, Outcome
from ray_training.runner import execute, failed_report, failure_code, write_report
from ray_training.spec import parse_spec
from ray_training.store import store_from_env


@ray.remote
def train(spec_text: str) -> dict:
    """Runs on the GPU worker. Never raises: a failure comes back as a failed report."""
    spec = parse_spec(spec_text)
    return execute(spec, store_from_env(spec.bucket, spec.prefix))


POLL_SECONDS = 1
timed_out = False


def on_alarm(*_):
    # Only set a flag: raising from a signal handler inside Ray's Cython ray.get ends in "SystemError: returned a result with an exception set".
    global timed_out
    timed_out = True


def wait(ref) -> dict:
    """ray.get, polled so the SIGALRM flag is seen; a cancelled or crashed task raises from the final ray.get."""
    while not ray.wait([ref], timeout=POLL_SECONDS)[0]:
        if timed_out:
            raise TimeoutError("job exceeded its time limit")
    return ray.get(ref)


def main(argv: list[str]) -> int:
    payload = json.loads(argv[1]).get("payload", {})
    store = store_from_env(payload.get("bucket", ""), payload.get("prefix", ""))
    report, spec, ref = failed_report(None, FailCode.INTERNAL, "driver ended before a report"), None, None
    try:
        try:
            spec = parse_spec(argv[1])
        except (ValueError, KeyError) as e:  # unknown version, kind, task or preset; or a missing field
            report = failed_report(None, FailCode.UNSUPPORTED, f"{type(e).__name__}: {e}")
            return 1
        signal.signal(signal.SIGALRM, on_alarm)
        signal.alarm(spec.max_runtime_s)
        ray.init(address="auto")
        gpus = float(os.environ.get(LOCAL_GPUS_ENV, DEFAULT_TASK_GPUS))  # 0 only in CI
        ref = train.options(num_gpus=gpus, num_cpus=spec.cpu, memory=spec.memory_bytes).remote(argv[1])
        report = wait(ref)  # blocks; a cancelled job raises here
    except BaseException as e:  # noqa: BLE001 - the report must always be written
        if ref is not None:
            ray.cancel(ref, force=True)
        report = failed_report(spec, failure_code(e), f"{type(e).__name__}: {e}")
    finally:
        signal.alarm(NO_ALARM)  # the timeout must not fire inside the report write
        write_report(store, report)
    return 1 if report["status"] == Outcome.FAILED else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
