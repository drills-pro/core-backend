"""POST /v1/predict to a deployment that still has a Serve app of its own (serving = legacy): the HTTP call to its route maps to the same
error table as a shared call, nothing is logged in predict_calls, and a call that reached Serve is stored under an Idempotency-Key."""
import httpx
import pytest

from app.integrations.serve_client import ServeErrorCode
from app.modules.user.deployments import model_predict, service, shared
from app.modules.user.deployments.constants import MODEL_REJECTED_DETAIL_MAX_CHARS, Serving
from tests.support.predict_env import RESOLVED, build

KEY = "legacy-key"
LEGACY = {**RESOLVED, "serving": Serving.LEGACY, "route_prefix": "/predict/legacy"}


@pytest.fixture
def env(monkeypatch):
    env = build(monkeypatch)
    env.conn.row = LEGACY
    env.posted, env.reply = [], httpx.Response(200, json={"predictions": [7]})

    async def post_to_serve(route_prefix, body):
        env.posted.append((route_prefix, body))
        if isinstance(env.reply, Exception):
            raise env.reply
        return env.reply, 4.0

    monkeypatch.setattr(service, "_post_to_serve", post_to_serve)
    return env


def logged_calls(env):
    return [sql for sql, _ in env.conn.calls if sql.startswith("INSERT INTO predict_calls")]


def test_a_legacy_call_posts_the_input_to_its_own_route_and_is_metered_but_not_logged_as_a_shared_call(env):
    r = env.post()
    assert r.status_code == 200 and r.json()["output"] == {"predictions": [7]} and r.json()["usage"] == {"latency_ms": 4.0}
    assert env.posted == [("/predict/legacy", {"text": "hello", "n": 1.0})] and not env.served and not logged_calls(env)
    assert len(env.usage) == 1


@pytest.mark.parametrize("reply,status,code", [
    (httpx.ReadTimeout("slow"), 504, "upstream_timeout"),
    (httpx.ConnectError("down"), 502, "upstream_error"),
    (httpx.Response(500, text="boom"), 502, "upstream_error"),
    (httpx.Response(503, text="no replica"), 502, "upstream_error"),
])
def test_a_legacy_failure_maps_like_a_shared_one_and_is_not_metered(env, reply, status, code):
    env.reply = reply
    r = env.post()
    assert (r.status_code, r.json()["code"]) == (status, code) and not env.usage


def test_a_legacy_4xx_is_the_models_refusal_with_its_text_cut_short(env):
    env.reply = httpx.Response(400, text="x" * (MODEL_REJECTED_DETAIL_MAX_CHARS + 50))
    r = env.post()
    assert (r.status_code, r.json()["code"]) == (422, "model_rejected_input")
    assert len(r.json()["detail"]) == MODEL_REJECTED_DETAIL_MAX_CHARS


def test_a_legacy_failure_under_a_key_is_stored_and_replayed_without_a_second_call(env):
    env.reply = httpx.Response(500, text="boom")
    first, second = env.post(key=KEY), env.post(key=KEY)
    assert (first.status_code, second.status_code) == (502, 502) and second.json()["code"] == "upstream_error"
    assert len(env.posted) == 1


def test_a_legacy_success_under_a_key_replays_the_stored_answer(env):
    first, second = env.post(key=KEY), env.post(key=KEY)
    assert first.json() == second.json() and len(env.posted) == 1 and len(env.usage) == 1


@pytest.mark.parametrize("code", list(ServeErrorCode))
def test_serve_problem_maps_every_code_and_only_the_models_refusal_shows_serves_words(code):
    problem = model_predict.serve_problem(shared.ServeFailed(code, "internal detail", 1.0))
    assert problem.status_code == {ServeErrorCode.BAD_INPUT: 422, ServeErrorCode.TIMEOUT: 504}.get(code, 502)  # unreachable is 502 here, 503 only on the legacy routes
    assert ("internal detail" in problem.detail) == (code == ServeErrorCode.BAD_INPUT)
