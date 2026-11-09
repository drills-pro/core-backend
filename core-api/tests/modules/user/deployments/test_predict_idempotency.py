"""POST /v1/predict with an Idempotency-Key (planning/24): scope, fingerprint, replay without a second Serve call, the race, stored failures
(Q3), the 24 h retention. The route, require_key and the store's Python are real; the predictions table is in memory."""
import asyncio
import uuid
from datetime import timedelta

import pytest

from app.core.authz import KeyContext, Scope
from app.core.settings import settings
from app.integrations import serve_client
from app.integrations.serve_client import ServeErrorCode
from app.core.problem import Problem
from app.modules.user.deployments import model_predict, prediction_store as store, shared
from app.modules.user.deployments.constants import PredictionMode, PredictionStatus, ProblemCode
from app.modules.user.deployments.schemas import PredictRequest
from tests.support.predict_env import BODY, MODEL, PROJECT, TENANT, build

KEY = "key-1"


@pytest.fixture
def env(monkeypatch):
    return build(monkeypatch)


def ctx(env):
    return KeyContext(uuid.uuid4(), str(TENANT), PROJECT, frozenset({Scope.PREDICT}), frozenset())


def seed_in_progress(env, key=KEY, body=BODY):
    """A first request that is still running: its row exists, in_progress, owned by a handler that has not finished."""
    request = PredictRequest.model_validate(body)
    hashed = model_predict.request_hash(request, MODEL, None)
    row, created = asyncio.run(store.begin(
        env.conn, ctx(env), uuid.uuid4(), MODEL, "3", request, mode=PredictionMode.SYNC, key=key, request_hash=hashed))
    assert created
    return row


def test_the_same_key_and_body_replays_the_stored_envelope_with_no_second_serve_call_and_no_second_usage_event(env):
    first, second = env.post(key=KEY), env.post(key=KEY)
    assert (first.status_code, second.status_code) == (200, 200) and first.json() == second.json()
    assert len(env.served) == 1 and len(env.usage) == 1
    (row,) = env.conn.preds.values()
    assert first.json()["id"] == f"pred_{row['id']}" and row["status"] == PredictionStatus.COMPLETED and row["mode"] == PredictionMode.SYNC


def test_the_replay_is_the_stored_output_not_a_new_answer(env):
    first = env.post(key=KEY).json()
    env.answer = lambda *a: (_ for _ in ()).throw(AssertionError("Serve must not be called again"))
    assert env.post(key=KEY).json()["output"] == first["output"] == {"predictions": [{"label": "cat"}]}


@pytest.mark.parametrize(
    "other", [{"input": {"text": "bye"}, "metadata": BODY["metadata"]}, {"input": BODY["input"], "metadata": {"trace": "t2"}}])
def test_the_same_key_with_another_body_is_422_idempotency_key_reuse(env, other):
    env.post(key=KEY)
    r = env.post(other, key=KEY)
    assert (r.status_code, r.json()["code"]) == (422, "idempotency_key_reuse") and len(env.served) == 1 and len(env.usage) == 1


def test_the_same_key_with_another_revision_is_422_idempotency_key_reuse(env):
    env.post(key=KEY, revision="3")
    r = env.post(key=KEY, revision="2")
    assert (r.status_code, r.json()["code"]) == (422, "idempotency_key_reuse") and len(env.served) == 1


def test_the_body_fingerprint_ignores_key_order_and_whitespace(env):
    a = PredictRequest.model_validate({"input": {"a": 1, "b": 2}, "metadata": {}})
    b = PredictRequest.model_validate({"metadata": {}, "input": {"b": 2, "a": 1}})
    assert model_predict.request_hash(a, MODEL, "1") == model_predict.request_hash(b, MODEL, "1") != model_predict.request_hash(a, MODEL, "2")


def test_two_projects_do_not_share_a_key(env):
    other = uuid.uuid4()
    first, second = env.post(key=KEY), env.post(key=KEY, secret=env.add_key(project=other), project=other)
    assert (first.status_code, second.status_code) == (200, 200) and first.json()["id"] != second.json()["id"] and len(env.served) == 2


def test_two_keys_of_one_project_share_the_namespace(env):
    first, second = env.post(key=KEY), env.post(key=KEY, secret=env.add_key())
    assert first.json() == second.json() and len(env.served) == 1


def test_a_call_without_a_key_stores_nothing(env):
    assert env.post().status_code == 200 and not env.conn.preds and not env.conn.events


@pytest.mark.parametrize("bad", ["", "k" * 256, "café"])
def test_422_for_a_key_that_is_empty_too_long_or_not_printable_ascii(env, bad):
    r = env.post(key=bad) if bad.isascii() else env.post(extra={"Idempotency-Key": bad.encode()})
    assert r.status_code == 422 and not env.served


def test_409_request_in_progress_with_retry_after_while_the_first_call_still_runs(env, monkeypatch):
    monkeypatch.setattr(settings.predict, "in_progress_wait_seconds", 0.05)
    seed_in_progress(env)
    r = env.post(key=KEY)
    assert (r.status_code, r.json()["code"]) == (409, "request_in_progress") and int(r.headers["Retry-After"]) >= 1 and not env.served and not env.usage


def test_a_retry_waits_for_the_first_call_and_replays_its_result_with_one_serve_call(env):
    async def slow(*args):
        await asyncio.sleep(0.3)
        return serve_client.Prediction(predictions=[{"label": "cat"}], cold_load=False), 5.0

    env.answer = slow

    async def go():
        request = PredictRequest.model_validate(BODY)
        args = (ctx(env), str(MODEL), None, request)
        return await asyncio.gather(
            model_predict.predict(*args, idempotency_key=KEY), model_predict.predict(*args, idempotency_key=KEY))

    first, second = asyncio.run(go())
    assert first.envelope.id == second.envelope.id and first.envelope.output == second.envelope.output
    assert len(env.served) == 1 and len(env.usage) == 1


def test_the_race_loser_reads_the_winners_row(env):
    """The insert that conflicts returns no row; the loser takes the winner's, so one prediction exists."""
    request = PredictRequest.model_validate(BODY)
    hashed = model_predict.request_hash(request, MODEL, None)

    async def go():
        args = (ctx(env), uuid.uuid4(), MODEL, "3", request)
        return [await store.begin(env.conn, *args, mode=PredictionMode.SYNC, key=KEY, request_hash=hashed) for _ in range(2)]

    (won, created), (lost, created_again) = asyncio.run(go())
    assert (created, created_again) == (True, False) and won["id"] == lost["id"] and len(env.conn.preds) == 1


@pytest.mark.parametrize(
    ("code", "status", "problem"),
    [(ServeErrorCode.BAD_INPUT, 422, "model_rejected_input"), (ServeErrorCode.TIMEOUT, 504, "upstream_timeout"), (ServeErrorCode.MODEL_LOAD_FAILED, 502, "upstream_error")],
)
def test_a_failed_serve_call_is_stored_and_the_retry_gets_the_same_failure_without_calling_serve(env, code, status, problem):
    def fail(*a):
        raise shared.ServeFailed(code, "internals", 3.0)

    env.answer = fail
    first = env.post(key=KEY)
    second = env.post(key=KEY)
    assert (first.status_code, first.json()["code"]) == (status, problem) and (second.status_code, second.json()["code"]) == (status, problem)
    assert first.json()["detail"] == second.json()["detail"] and len(env.served) == 1 and not env.usage
    (row,) = env.conn.preds.values()
    assert row["status"] == PredictionStatus.FAILED and row["error"]["code"] == problem


def test_a_new_key_runs_the_call_again_after_a_stored_failure(env):
    def fail(*a):
        raise shared.ServeFailed(ServeErrorCode.UNREACHABLE, "down", 3.0)

    env.answer = fail
    env.post(key="a")
    env.answer = lambda *a: (serve_client.Prediction(predictions=[1], cold_load=False), 1.0)
    assert env.post(key="b").status_code == 200 and len(env.served) == 2


def test_a_failure_before_serve_is_not_stored_and_frees_the_key(env):
    def refuse(*a):
        raise Problem(409, ProblemCode.NO_SERVABLE_MODEL, "no model")

    env.answer = refuse
    assert env.post(key=KEY).status_code == 409
    env.answer = lambda *a: (serve_client.Prediction(predictions=[1], cold_load=False), 1.0)
    assert env.post(key=KEY).status_code == 200 and len(env.served) == 2 and len(env.usage) == 1


def test_a_call_whose_handler_died_is_failed_after_its_lease_and_the_retry_gets_a_502(env):
    row = seed_in_progress(env)
    env.conn.expire_lease(row["id"])
    r = env.post(key=KEY)
    assert (r.status_code, r.json()["code"]) == (502, "upstream_error") and not env.served
    assert env.conn.preds[row["id"]]["status"] == PredictionStatus.FAILED


def test_a_stored_sync_result_is_kept_for_24_hours(env):
    env.post(key=KEY)
    (row,) = env.conn.preds.values()
    assert settings.predict.sync_retention_hours == 24
    assert abs((row["expires_at"] - row["finished_at"]) - timedelta(hours=24)) < timedelta(seconds=5)


def test_every_status_change_is_an_event(env):
    env.post(key=KEY)
    (prediction_id,) = env.conn.preds
    assert env.conn.statuses(prediction_id) == [PredictionStatus.IN_PROGRESS, PredictionStatus.COMPLETED]
