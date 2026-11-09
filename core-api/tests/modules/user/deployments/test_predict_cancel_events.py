"""POST /v1/predict/{id}/cancel and GET /v1/predict/{id}/events (planning/24): every state a cancel can find, the worker honouring it, and
the event stream with Last-Event-ID replay and heartbeat. The route, require_key, the store and the worker are real; Serve and the table are faked."""
import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest

from app.core.authz import KeyContext, Scope
from app.integrations import serve_client
from app.modules.user.deployments import prediction_service as service, prediction_worker as worker, shared
from app.modules.user.deployments.constants import (
    CANCEL_CHECK_SECONDS,
    SSE_HEARTBEAT_SECONDS,
    SSE_POLL_SECONDS,
    PredictionStatus,
)
from tests.support.predict_env import PROJECT, TENANT, build

ASYNC = {"Prefer": "respond-async"}
OK = (serve_client.Prediction(predictions=[{"label": "cat"}], cold_load=False), 2.0)


@pytest.fixture
def env(monkeypatch):
    return build(monkeypatch)


def queue(env, key="k"):
    posted = env.post(key=key, extra=ASYNC).json()
    return posted["id"], env.conn.preds[uuid.UUID(posted["id"].removeprefix("pred_"))]


def run(coro):
    return asyncio.run(coro)


def frames(text: str) -> list[tuple[int, str, dict]]:
    """(id, event, data) of each event in a text/event-stream body; comments are dropped."""
    out = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if not line.startswith(":"))
        if fields:
            out.append((int(fields["id"]), fields["event"], json.loads(fields["data"])))
    return out


def ctx():
    return KeyContext(uuid.uuid4(), str(TENANT), PROJECT, frozenset({Scope.PREDICT}), frozenset())


# --- cancel -------------------------------------------------------------------------------------------------------------------------------


def test_cancelling_a_queued_prediction_ends_it_at_once_and_the_worker_never_runs_it(env):
    prediction_id, row = queue(env)
    r = env.cancel(prediction_id)
    assert r.status_code == 200 and r.json()["status"] == "cancelled" and r.json()["id"] == prediction_id and r.json()["output"] is None
    assert (row["status"], row["cancel_requested"]) == (PredictionStatus.CANCELLED, False) and row["finished_at"] and row["expires_at"]
    assert env.conn.statuses(row["id"]) == [PredictionStatus.QUEUED, PredictionStatus.CANCELLED]
    run(worker.process_predictions())
    assert not env.served and not env.usage and env.get(prediction_id).json()["status"] == "cancelled"


def test_cancelling_a_running_prediction_sets_the_flag_and_leaves_it_to_its_worker(env):
    prediction_id, row = queue(env)
    run(worker.store.claim(env.conn, str(TENANT), 1, "w"))
    r = env.cancel(prediction_id)
    assert r.status_code == 200 and r.json()["status"] == "in_progress" and (row["status"], row["cancel_requested"]) == (PredictionStatus.IN_PROGRESS, True)


@pytest.mark.parametrize(
    ("status", "fields"),
    [
        (PredictionStatus.COMPLETED, {"output": {"o": 1}}),
        (PredictionStatus.FAILED, {"error": {"code": "upstream_error", "detail": "d"}}),
        (PredictionStatus.CANCELLED, {}),
    ],
)
def test_cancelling_a_finished_prediction_is_an_idempotent_200_that_changes_nothing(env, status, fields):
    prediction_id, row = queue(env)
    row.update(status=status, **fields)
    before = env.get(prediction_id).json()
    for _ in range(2):
        r = env.cancel(prediction_id)
        assert r.status_code == 200 and r.json() == before
    assert row["status"] == status and row["cancel_requested"] is False


def test_cancelling_twice_is_the_same_200(env):
    prediction_id, _ = queue(env)
    assert env.cancel(prediction_id).json() == env.cancel(prediction_id).json()


def test_another_key_the_unknown_id_and_a_malformed_id_get_the_one_404_on_cancel(env):
    prediction_id, row = queue(env)
    other = env.add_key()
    answers = [env.cancel(prediction_id, secret=other), env.cancel(f"pred_{uuid.uuid4()}"), env.cancel("nonsense")]
    assert {r.status_code for r in answers} == {404} and len({json.dumps({k: v for k, v in r.json().items() if k != "request_id"}) for r in answers}) == 1
    assert row["status"] == PredictionStatus.QUEUED  # key B did not cancel key A's prediction


def test_cancel_needs_a_valid_key_of_the_project(env):
    prediction_id, _ = queue(env)
    assert env.cancel(prediction_id, secret="drl_nope").status_code == 401
    assert env.cancel(prediction_id, project=uuid.uuid4()).status_code == 401


def test_a_result_that_arrives_after_the_cancel_flag_is_discarded(env):
    prediction_id, row = queue(env)

    def answer(*a):
        row["cancel_requested"] = True  # the key cancels while Serve is working
        return OK

    env.answer = answer
    run(worker.process_predictions())
    assert (row["status"], row["output"], row["attempt"]) == (PredictionStatus.CANCELLED, None, 1) and not env.usage
    assert env.conn.statuses(row["id"]) == [PredictionStatus.QUEUED, PredictionStatus.IN_PROGRESS, PredictionStatus.CANCELLED]


def test_a_failure_after_the_cancel_flag_ends_cancelled_not_requeued(env):
    _, row = queue(env)

    def answer(*a):
        row["cancel_requested"] = True
        raise shared.ServeFailed(serve_client.ServeErrorCode.MODEL_LOAD_FAILED, "x", 1.0)

    env.answer = answer
    run(worker.process_predictions())
    assert row["status"] == PredictionStatus.CANCELLED and row["error"] is None


def test_the_worker_checks_the_flag_before_each_attempt(env):
    """Cancelled while it waited for a retry: the next claim ends it without a Serve call."""
    _, row = queue(env)
    row.update(cancel_requested=True)
    run(worker.process_predictions())
    assert (row["status"], row["attempt"]) == (PredictionStatus.CANCELLED, 1) and not env.served and not env.usage


def test_the_worker_drops_the_serve_call_when_the_key_cancels_while_it_waits(env, monkeypatch):
    monkeypatch.setattr(worker, "CANCEL_CHECK_SECONDS", 0.02)
    _, row = queue(env)
    state = {"cancelled": False}

    async def hang(*a):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["cancelled"] = True  # httpx's request is cancelled with it
            raise

    env.answer = hang

    async def go():
        async def key_cancels():
            await asyncio.sleep(0.1)
            row["cancel_requested"] = True

        task = asyncio.create_task(key_cancels())
        started = asyncio.get_running_loop().time()
        await worker.process_predictions()
        await task
        return asyncio.get_running_loop().time() - started

    assert run(go()) < 5 and state["cancelled"]
    assert (row["status"], row["output"]) == (PredictionStatus.CANCELLED, None) and not env.usage


def test_the_cancel_check_interval_is_two_seconds():
    assert CANCEL_CHECK_SECONDS == 2


# --- events -------------------------------------------------------------------------------------------------------------------------------


def finished(env):
    """A background prediction that ran to completion: events queued, in_progress, completed."""
    env.answer = lambda *a: OK
    prediction_id, row = queue(env)
    run(worker.process_predictions())
    return prediction_id, row


def test_the_stream_replays_every_event_in_order_with_its_seq_and_ends_after_the_terminal_one(env):
    prediction_id, _ = finished(env)
    r = env.get(prediction_id, path="/events")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    got = frames(r.text)
    assert [(i, e) for i, e, _ in got] == [(1, "queued"), (2, "in_progress"), (3, "completed")]
    assert got[0][2] == {"status": "queued"} and got[2][2]["id"] == prediction_id and got[2][2]["output"] == {"predictions": [{"label": "cat"}]}


@pytest.mark.parametrize(("last", "expected"), [("0", [1, 2, 3]), ("1", [2, 3]), ("2", [3]), ("3", []), ("abc", [1, 2, 3]), ("-1", [1, 2, 3])])
def test_last_event_id_replays_everything_after_it(env, last, expected):
    prediction_id, _ = finished(env)
    r = env.get(prediction_id, path="/events", extra={"Last-Event-ID": last})
    assert [i for i, _, _ in frames(r.text)] == expected


def test_a_cancelled_prediction_streams_its_cancel(env):
    prediction_id, _ = queue(env)
    env.cancel(prediction_id)
    assert [e for _, e, _ in frames(env.get(prediction_id, path="/events").text)] == ["queued", "cancelled"]


def test_the_stream_of_another_keys_or_an_unknown_prediction_is_the_one_404(env):
    prediction_id, _ = finished(env)
    for r in (env.get(prediction_id, path="/events", secret=env.add_key()), env.get(f"pred_{uuid.uuid4()}", path="/events"), env.get("x", path="/events")):
        assert r.status_code == 404 and r.json()["code"] == "not_found" and r.headers["content-type"].startswith("application/problem+json")
    assert env.get(prediction_id, path="/events", secret="drl_nope").status_code == 401


def test_a_purged_prediction_has_no_events_and_the_stream_ends_at_once(env):
    prediction_id, row = finished(env)
    env.conn.events[:] = [e for e in env.conn.events if e[0] != row["id"]]
    row.update(input=None, output=None, purged_at=datetime.now(timezone.utc))
    assert env.get(prediction_id, path="/events").text == ""


def test_the_stream_follows_a_live_prediction_and_resumes_after_a_drop_delivering_each_event_once_in_order(env, monkeypatch):
    monkeypatch.setattr(service, "SSE_POLL_SECONDS", 0.01)
    _, row = queue(env)

    async def collect(after, stop_after=None):
        got = []
        async for text in service.follow(ctx(), row["id"], after):
            got += frames(text)
            if stop_after and len(got) >= stop_after:
                break  # the connection drops
        return got

    async def go():
        async def progress():
            await asyncio.sleep(0.05)
            run_claim = await worker.store.claim(env.conn, str(TENANT), 1, "w")
            await asyncio.sleep(0.05)
            async with worker.TenantConn(str(TENANT)) as conn:
                await worker.store.finish(conn, run_claim[0], PredictionStatus.COMPLETED, output={"o": 1}, latency_ms=1.0)

        updater = asyncio.create_task(progress())
        first = await collect(0, stop_after=2)  # sees queued, in_progress, then the client goes away
        resumed = await collect(first[-1][0])  # Last-Event-ID: the last it saw
        await updater
        return first, resumed

    first, resumed = run(go())
    seen = first + resumed
    assert [i for i, _, _ in seen] == [1, 2, 3] and [e for _, e, _ in seen] == ["queued", "in_progress", "completed"]
    assert resumed[-1][2]["output"] == {"o": 1}


def test_a_quiet_stream_sends_a_heartbeat_comment_and_still_ends_with_the_terminal_event(env, monkeypatch):
    monkeypatch.setattr(service, "SSE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(service, "SSE_HEARTBEAT_SECONDS", 0.05)
    _, row = queue(env)

    async def go():
        texts = []

        async def finish_later():
            await asyncio.sleep(0.2)
            env.conn.preds[row["id"]]["status"] = PredictionStatus.CANCELLED
            env.conn.events.append((row["id"], PredictionStatus.CANCELLED, {"status": "cancelled"}))

        updater = asyncio.create_task(finish_later())
        async for text in service.follow(ctx(), row["id"], 1):  # the queued event was already seen
            texts.append(text)
        await updater
        return texts

    texts = run(go())
    assert texts[0] == ": heartbeat\n\n" and texts.count(": heartbeat\n\n") >= 2 and texts[-1].startswith("id: 2\nevent: cancelled")


def test_the_stream_timing_is_a_one_second_poll_and_a_fifteen_second_heartbeat():
    assert (SSE_POLL_SECONDS, SSE_HEARTBEAT_SECONDS) == (1, 15)
