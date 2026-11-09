"""Accept: text/event-stream on POST /v1/predict (planning/24, Q4): 406 not_acceptable while no serve adapter declares streaming. No SSE tokens are built."""
import pytest

from tests.support.predict_env import build


@pytest.fixture
def env(monkeypatch):
    return build(monkeypatch)


@pytest.mark.parametrize("accept", ["text/event-stream", "TEXT/Event-Stream; q=1", "text/event-stream, text/plain"])
def test_asking_only_for_a_stream_is_406_problem_json_and_nothing_runs(env, accept):
    r = env.post(extra={"Accept": accept})
    assert (r.status_code, r.json()["code"]) == (406, "not_acceptable") and r.headers["content-type"].startswith("application/problem+json")
    assert not env.served and not env.usage and not env.checks and not env.conn.preds


def test_a_stream_with_a_background_key_is_still_406_and_stores_nothing(env):
    r = env.post(key="k1", extra={"Accept": "text/event-stream", "Prefer": "respond-async"})
    assert r.status_code == 406 and not env.conn.preds


@pytest.mark.parametrize("accept", [None, "application/json", "*/*", "text/event-stream, application/json;q=0.5", "text/html"])
def test_any_accept_that_takes_json_or_does_not_ask_for_a_stream_is_the_sync_answer(env, accept):
    r = env.post(extra={"Accept": accept} if accept else None)
    assert r.status_code == 200 and len(env.served) == 1


def test_the_key_is_checked_before_the_accept_header(env):
    r = env.post(secret="drl_nope", extra={"Accept": "text/event-stream"})
    assert r.status_code == 401
