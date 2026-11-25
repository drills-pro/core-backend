"""The prediction worker (planning/24): claim, concurrent dispatch, fenced write-back, retry with backoff, lost leases, the poller wiring.
Serve, the clock-free parts of the database and the predictions table are faked; the worker's and the store's Python are real."""
import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import background
from app.core.problem import Problem
from app.core.settings import settings
from app.integrations import serve_client
from app.integrations.serve_client import ServeErrorCode
from app.modules.user.deployments import prediction_store as store, prediction_worker as worker
from app.modules.user.deployments.predict import ServeFailed
from app.modules.user.deployments.constants import RETRY_BACKOFF_SECONDS, PredictionStatus
from app.modules.user.deployments.status import DeploymentStatus
from tests.support.predict_env import BODY, RESOLVED, TENANT, build

ASYNC = {"Prefer": "respond-async"}
OK = (serve_client.Prediction(predictions=[{"label": "cat"}], cold_load=False), 7.5)


@pytest.fixture
def env(monkeypatch):
    return build(monkeypatch)


def queue(env, n=1, body=BODY):
    """n accepted background predictions; their rows, oldest first."""
    for i in range(n):
        assert env.post(body, key=f"k{len(env.conn.preds)}", extra=ASYNC).status_code == 202
    return list(env.conn.preds.values())[-n:]


def run(coro):
    return asyncio.run(coro)


def serve_fails(code):
    def fail(*a):
        raise ServeFailed(code, "internals", 1.0, None, 0)

    return fail


def due(row):
    row["next_attempt_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)


def test_a_queued_prediction_is_claimed_run_and_completed_with_one_usage_event(env):
    env.answer = lambda *a: OK
    (row,) = queue(env)
    run(worker.process_predictions())
    assert (row["status"], row["attempt"], row["output"], row["latency_ms"]) == (PredictionStatus.COMPLETED, 1, {"predictions": [{"label": "cat"}]}, 7.5)
    assert row["lease_expires_at"] is None and row["finished_at"] and row["expires_at"] - row["finished_at"] > timedelta(days=6, hours=23)
    assert env.conn.statuses(row["id"]) == [PredictionStatus.QUEUED, PredictionStatus.IN_PROGRESS, PredictionStatus.COMPLETED]
    (usage,) = env.usage
    assert usage["source_ref"] == str(row["id"])
    (args,) = env.served
    assert args[4] == BODY["input"] and args[5] == settings.predict.worker_concurrency  # input as stored; reuse of predict.call


def test_the_claim_takes_a_lease_and_counts_the_attempt(env):
    (row,) = queue(env)
    claimed = run(store.claim(env.conn, str(TENANT), 5, "me"))
    assert [c["id"] for c in claimed] == [row["id"]] and (row["status"], row["attempt"], row["lease_owner"]) == (PredictionStatus.IN_PROGRESS, 1, "me")
    assert row["lease_expires_at"] > datetime.now(timezone.utc) + timedelta(seconds=settings.predict.lease_seconds - 5)


def test_two_claims_never_take_the_same_prediction_and_a_claimed_one_is_not_due(env):
    rows = queue(env, 6)
    first = run(store.claim(env.conn, str(TENANT), 4, "a"))
    second = run(store.claim(env.conn, str(TENANT), 4, "b"))
    assert len(first) == 4 and len(second) == 2 and {r["id"] for r in first}.isdisjoint(r["id"] for r in second)
    assert {r["id"] for r in first + second} == {r["id"] for r in rows} and run(store.claim(env.conn, str(TENANT), 4, "c")) == []


def test_a_claim_leaves_a_prediction_that_is_waiting_for_its_backoff(env):
    (row,) = queue(env)
    row["next_attempt_at"] = datetime.now(timezone.utc) + timedelta(seconds=30)
    assert run(store.claim(env.conn, str(TENANT), 5, "a")) == [] and row["status"] == PredictionStatus.QUEUED


def test_a_burst_fans_out_concurrently_up_to_worker_concurrency(env, monkeypatch):
    """Twelve predictions, four at a time: the peak in flight is the knob, not one, and the burst finishes in waves, not in a row."""
    monkeypatch.setattr(settings.predict, "worker_concurrency", 4)
    live = peak = 0

    async def slow(*args):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.05)
        live -= 1
        return OK

    env.answer = slow
    rows = queue(env, 12)
    started = time.monotonic()
    run(worker.process_predictions())
    assert peak == 4 and all(r["status"] == PredictionStatus.COMPLETED for r in rows) and len(env.usage) == 12
    assert time.monotonic() - started < 12 * 0.05 * 0.8  # a serial run would take 0.6 s


def test_the_semaphore_bounds_a_round_that_claimed_more_than_the_concurrency(env, monkeypatch):
    """Many tenants each get at least one claim, so a round can hold more rows than the knob: they still run only `worker_concurrency` at a time."""
    monkeypatch.setattr(settings.predict, "worker_concurrency", 2)
    live = peak = 0

    async def slow(*args):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1
        return OK

    env.answer = slow
    queue(env, 6)
    sem = asyncio.Semaphore(2)

    async def go():
        claimed = await store.claim(env.conn, str(TENANT), 6, "me")
        await asyncio.gather(*(worker._run(sem, row) for row in claimed))

    run(go())
    assert peak == 2


@pytest.mark.parametrize("code", [ServeErrorCode.TIMEOUT, ServeErrorCode.MODEL_LOAD_FAILED, ServeErrorCode.UNREACHABLE])
def test_a_5xx_goes_back_to_the_queue_with_a_backoff_and_fails_after_max_attempts(env, code):
    env.answer = serve_fails(code)
    (row,) = queue(env)
    expected_code = "upstream_timeout" if code == ServeErrorCode.TIMEOUT else "upstream_error"
    for attempt, delay in enumerate(RETRY_BACKOFF_SECONDS[:2], start=1):
        run(worker.process_predictions())
        assert (row["status"], row["attempt"], row["error"]) == (PredictionStatus.QUEUED, attempt, None)
        wait = (row["next_attempt_at"] - datetime.now(timezone.utc)).total_seconds()
        assert delay - 2 < wait <= delay and row["lease_expires_at"] is None
        run(worker.process_predictions())  # not due yet: nothing is claimed
        assert row["attempt"] == attempt
        due(row)
    run(worker.process_predictions())
    assert (row["status"], row["attempt"], row["error"]["code"]) == (PredictionStatus.FAILED, 3, expected_code) and row["finished_at"]
    assert len(env.served) == 3 and not env.usage
    assert env.conn.statuses(row["id"]) == [PredictionStatus.QUEUED] + [PredictionStatus.IN_PROGRESS, PredictionStatus.QUEUED] * 2 + [PredictionStatus.IN_PROGRESS, PredictionStatus.FAILED]


def test_the_backoff_is_5_30_and_120_seconds_and_the_last_repeats():
    assert [worker.backoff_seconds(a) for a in (1, 2, 3, 4, 9)] == [5, 30, 120, 120, 120]


def test_a_4xx_fails_at_once_without_a_retry(env):
    env.answer = serve_fails(ServeErrorCode.BAD_INPUT)
    (row,) = queue(env)
    run(worker.process_predictions())
    assert (row["status"], row["attempt"], row["error"]["code"]) == (PredictionStatus.FAILED, 1, "model_rejected_input") and len(env.served) == 1 and not env.usage


def test_a_failure_that_is_not_a_serve_answer_is_retried_as_a_5xx(env):
    def boom(*a):
        raise RuntimeError("bug")

    env.answer = boom
    (row,) = queue(env)
    run(worker.process_predictions())
    assert (row["status"], row["attempt"]) == (PredictionStatus.QUEUED, 1)


def test_too_many_in_flight_is_retried_and_no_servable_model_fails(env):
    (retried, failed) = queue(env, 2)
    env.answer = lambda *a: (_ for _ in ()).throw(Problem(429, "too_many_inflight", "busy"))
    run(worker.process_predictions())
    assert retried["status"] == failed["status"] == PredictionStatus.QUEUED
    failed.update(status=PredictionStatus.QUEUED, attempt=0, next_attempt_at=datetime.now(timezone.utc))
    retried.update(next_attempt_at=datetime.now(timezone.utc) + timedelta(hours=1))
    env.answer = lambda *a: (_ for _ in ()).throw(Problem(409, "no_servable_model", "none"))
    run(worker.process_predictions())
    assert (failed["status"], failed["error"]["code"]) == (PredictionStatus.FAILED, "no_servable_model")


@pytest.mark.parametrize("status", [DeploymentStatus.STOPPED, DeploymentStatus.DELETED, None])
def test_a_prediction_whose_deployment_is_no_longer_active_fails_without_a_serve_call(env, status):
    (row,) = queue(env)
    env.conn.row = None if status is None else {**RESOLVED, "status": status}
    run(worker.process_predictions())
    assert (row["status"], row["error"]["code"]) == (PredictionStatus.FAILED, "deployment_not_active") and not env.served


def test_a_stale_attempt_cannot_overwrite_the_current_ones_result(env):
    """Worker A claims, loses its lease and is replaced by worker B; when A's Serve call returns late its write-back is dropped."""
    (row,) = queue(env)
    (a_row,) = run(store.claim(env.conn, str(TENANT), 1, "A"))
    a_row = dict(a_row)  # what A holds: attempt 1
    env.conn.expire_lease(row["id"])
    run(worker._reap(str(TENANT)))  # the lease ran out: back to queued (attempt 1 < 3)
    assert row["status"] == PredictionStatus.QUEUED
    due(row)
    env.answer = lambda *a: (serve_client.Prediction(predictions=["B"], cold_load=False), 1.0)
    run(worker.process_predictions())  # B: attempt 2, completes
    assert (row["status"], row["attempt"], row["output"]) == (PredictionStatus.COMPLETED, 2, {"predictions": ["B"]})
    env.answer = lambda *a: (serve_client.Prediction(predictions=["A"], cold_load=False), 9.0)
    run(worker._attempt(a_row))  # A answers late: fenced out
    assert (row["status"], row["output"], row["latency_ms"]) == (PredictionStatus.COMPLETED, {"predictions": ["B"]}, 1.0)
    assert len(env.usage) == 1 and env.conn.statuses(row["id"]).count(PredictionStatus.COMPLETED) == 1


def test_a_stale_attempts_failure_and_retry_are_dropped_too(env):
    (row,) = queue(env)
    (a_row,) = run(store.claim(env.conn, str(TENANT), 1, "A"))
    a_row = dict(a_row)
    env.conn.expire_lease(row["id"])
    run(worker._reap(str(TENANT)))
    due(row)
    env.answer = lambda *a: OK
    run(worker.process_predictions())
    env.answer = serve_fails(ServeErrorCode.MODEL_LOAD_FAILED)
    run(worker._attempt(a_row))
    assert (row["status"], row["attempt"], row["error"]) == (PredictionStatus.COMPLETED, 2, None)


def test_a_prediction_whose_worker_died_returns_to_the_queue_and_runs_again(env):
    (row,) = queue(env)
    run(store.claim(env.conn, str(TENANT), 1, "dead"))  # claimed, and the process is gone: no write-back ever comes
    assert row["status"] == PredictionStatus.IN_PROGRESS
    run(worker.process_predictions())  # lease still held: nothing to do
    assert (row["status"], row["attempt"]) == (PredictionStatus.IN_PROGRESS, 1) and not env.served
    env.conn.expire_lease(row["id"])
    run(worker._reap(str(TENANT)))
    assert row["status"] == PredictionStatus.QUEUED and row["lease_owner"] is None and row["next_attempt_at"] > datetime.now(timezone.utc)
    due(row)
    env.answer = lambda *a: OK
    run(worker.process_predictions())
    assert (row["status"], row["attempt"]) == (PredictionStatus.COMPLETED, 2)


def test_a_prediction_that_keeps_losing_its_worker_fails_after_max_attempts(env):
    (row,) = queue(env)
    for _ in range(3):
        due(row)
        run(store.claim(env.conn, str(TENANT), 1, "dead"))
        env.conn.expire_lease(row["id"])
        run(worker._reap(str(TENANT)))
    assert (row["status"], row["attempt"], row["error"]["code"]) == (PredictionStatus.FAILED, 3, "upstream_error") and not env.served and not env.usage


def test_one_predictions_failure_does_not_stop_the_others(env):
    rows = queue(env, 3)

    def answer(*a):
        if len(env.served) == 2:
            raise ServeFailed(ServeErrorCode.BAD_INPUT, "no", 1.0, None, 0)
        return OK

    env.answer = answer
    run(worker.process_predictions())
    assert sorted(r["status"] for r in rows) == [PredictionStatus.COMPLETED, PredictionStatus.COMPLETED, PredictionStatus.FAILED]


def test_the_worker_does_nothing_without_work(env):
    run(worker.process_predictions())
    assert not env.served


def test_the_pollers_are_registered_and_the_worker_elects_a_leader():
    pollers = {name: (fn, interval) for name, fn, interval in background.POLLERS}
    assert pollers["prediction-worker"] == (worker.process_predictions, 1) and pollers["prediction-purge"] == (worker.purge_stored_predictions, background.PURGE_INTERVAL_SECONDS)
    assert {"prediction-worker", "prediction-purge"}.isdisjoint(background.CLAIMING)


def test_the_knobs_are_config_with_the_planning_defaults():
    p = settings.predict
    assert (p.worker_concurrency, p.max_attempts, p.background_retention_days, p.sync_retention_hours) == (8, 3, 7, 24) and p.lease_seconds > settings.serving.request_timeout_seconds
