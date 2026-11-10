"""The prediction worker and the sync idempotent path when the storage under them fails: an output the bucket refuses, a write-back the
database refuses, a stored row another handler already ended."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.integrations import serve_client
from app.modules.user.deployments import model_predict, prediction_store as store, prediction_worker as worker
from app.modules.user.deployments.constants import PredictionStatus
from tests.support.predict_env import BODY, build

ASYNC = {"Prefer": "respond-async"}
OK = (serve_client.Prediction(predictions=[{"label": "cat"}], cold_load=False), 7.5)
KEY = "k-sync"


@pytest.fixture
def env(monkeypatch):
    env = build(monkeypatch)
    env.answer = lambda *a: OK
    return env


def queue(env):
    assert env.post(BODY, key="k0", extra=ASYNC).status_code == 202
    return next(iter(env.conn.preds.values()))


def due(row):
    row["next_attempt_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)


def test_an_output_the_bucket_refuses_is_retried_like_any_failed_attempt_and_fails_after_the_last(env, monkeypatch):
    async def refuse(row, value):
        raise OSError("bucket down")

    monkeypatch.setattr(store, "stash_output", refuse)
    row = queue(env)
    for attempt in (1, 2):
        asyncio.run(worker.process_predictions())
        assert (row["status"], row["attempt"]) == (PredictionStatus.QUEUED, attempt)
        due(row)
    asyncio.run(worker.process_predictions())
    assert (row["status"], row["attempt"], row["error"]["code"]) == (PredictionStatus.FAILED, 3, "upstream_error")
    assert row["output"] is None and not env.usage  # the model answered, but nothing the caller could read exists, so nothing is billed


def test_a_write_back_the_database_refuses_leaves_the_lease_for_the_reaper_and_does_not_stop_the_round(env, monkeypatch):
    real = store.finish
    calls = []

    async def flaky(conn, row, status, **kw):
        calls.append(status)
        if len(calls) == 1:
            raise ConnectionError("connection lost")
        return await real(conn, row, status, **kw)

    monkeypatch.setattr(store, "finish", flaky)
    first = queue(env)
    assert env.post(BODY, key="k1", extra=ASYNC).status_code == 202
    second = [r for r in env.conn.preds.values() if r is not first][0]
    asyncio.run(worker.process_predictions())  # does not raise
    assert sorted((first["status"], second["status"])) == [PredictionStatus.COMPLETED, PredictionStatus.IN_PROGRESS]
    stuck = first if first["status"] == PredictionStatus.IN_PROGRESS else second
    assert stuck["lease_expires_at"] is not None  # still leased: the reaper requeues it once the lease runs out
    env.conn.expire_lease(stuck["id"])
    asyncio.run(worker.process_predictions())
    assert stuck["status"] in (PredictionStatus.QUEUED, PredictionStatus.COMPLETED)


def test_a_sync_call_whose_lease_ran_out_under_it_answers_what_the_retry_stored(env, monkeypatch):
    """The handler's own write-back finds the row already ended (finish returns None): it answers the stored row, not its own output."""
    real = store.finish

    async def lost_race(conn, row, status, **kw):
        if status == PredictionStatus.COMPLETED:
            await real(conn, row, status, output={"predictions": [{"label": "dog"}]}, latency_ms=1.0)  # a retry ended it first
            return None
        return await real(conn, row, status, **kw)

    monkeypatch.setattr(model_predict.store, "finish", lost_race)
    r = env.post(key=KEY)
    assert r.status_code == 200 and r.json()["output"] == {"predictions": [{"label": "dog"}]}
