"""Background mode (planning/24): `Prefer: respond-async` returns 202 + Location + Preference-Applied with a queued row, and GET reads it back,
scoped to the key that made it. The route, require_key and the store's Python are real; the predictions table is in memory."""
import uuid
from datetime import datetime, timezone

import pytest

from app.modules.user.deployments.constants import PredictionMode, PredictionStatus
from tests.support.predict_env import BODY, RESOLVED, build

ASYNC = {"Prefer": "respond-async"}
KEY = "bg-key"


@pytest.fixture
def env(monkeypatch):
    return build(monkeypatch)


def accept(env, key=KEY, body=BODY, **kw):
    return env.post(body, key=key, extra={**ASYNC, **kw.pop("extra", {})}, **kw)


def test_202_with_location_and_preference_applied_and_a_queued_row_and_no_serve_call(env):
    r = accept(env)
    body = r.json()
    assert r.status_code == 202 and body["status"] == "queued" and body["output"] is None and body["error"] is None
    assert r.headers["Location"] == f"/v1/predict/{body['id']}" and r.headers["Preference-Applied"] == "respond-async"
    (row,) = env.conn.preds.values()
    assert (row["status"], row["mode"], row["attempt"], row["input"], row["metadata"]) == (PredictionStatus.QUEUED, PredictionMode.BACKGROUND, 0, BODY["input"], BODY["metadata"])
    assert row["idempotency_key"] == KEY and row["max_attempts"] == 3 and row["lease_expires_at"] is None
    assert body["id"] == f"pred_{row['id']}" and not env.served and not env.usage
    assert env.conn.statuses(row["id"]) == [PredictionStatus.QUEUED]


@pytest.mark.parametrize("prefer", ["respond-async", "Respond-Async", "return=minimal, respond-async", "respond-async; wait=5"])
def test_the_prefer_header_is_read_as_a_list_of_preferences(env, prefer):
    assert env.post(key=KEY, extra={"Prefer": prefer}).status_code == 202


@pytest.mark.parametrize("prefer", ["wait=10", "return=minimal", ""])
def test_another_preference_leaves_the_call_sync(env, prefer):
    assert env.post(key=KEY, extra={"Prefer": prefer}).status_code == 200


def test_422_without_an_idempotency_key_and_nothing_is_stored(env):
    r = env.post(extra=ASYNC)
    assert (r.status_code, r.json()["code"]) == (422, "validation_error") and "Idempotency-Key" in r.json()["detail"] and not env.conn.preds


def test_the_same_key_and_body_answers_202_with_the_same_prediction_and_stores_one_row(env):
    first, second = accept(env), accept(env)
    assert (first.status_code, second.status_code) == (202, 202) and first.json() == second.json() and len(env.conn.preds) == 1
    assert second.headers["Location"] == first.headers["Location"]


def test_the_same_key_with_another_body_is_422_idempotency_key_reuse(env):
    accept(env)
    r = accept(env, body={"input": "other"})
    assert (r.status_code, r.json()["code"]) == (422, "idempotency_key_reuse") and len(env.conn.preds) == 1


def test_a_replay_after_the_prediction_finished_is_200_with_the_result(env):
    first = accept(env).json()
    (row,) = env.conn.preds.values()
    row.update(status=PredictionStatus.COMPLETED, output={"o": 1}, latency_ms=3.0, finished_at=datetime.now(timezone.utc))
    r = accept(env)
    assert r.status_code == 200 and r.json()["id"] == first["id"] and r.json()["status"] == "completed" and r.json()["output"] == {"o": 1} and "Location" not in r.headers


def test_a_sync_retry_with_the_key_of_a_background_prediction_gets_it_back(env):
    first = accept(env).json()
    r = env.post(key=KEY)
    assert r.status_code == 202 and r.json()["id"] == first["id"] and not env.served


def test_the_accept_path_keeps_the_sync_errors(env):
    env.conn.row = None
    r = accept(env)
    assert (r.status_code, r.json()["code"]) == (404, "not_found") and not env.conn.preds
    env.limited = True
    env.conn.row = RESOLVED
    assert accept(env).status_code == 429 and not env.conn.preds


def test_get_returns_the_envelope_of_the_prediction_the_key_made(env):
    posted = accept(env).json()
    r = env.get(posted["id"])
    assert r.status_code == 200 and r.json() == posted


@pytest.mark.parametrize(
    ("status", "fields"),
    [
        (PredictionStatus.COMPLETED, {"output": {"predictions": [1]}, "latency_ms": 4.0}),
        (PredictionStatus.FAILED, {"error": {"code": "upstream_error", "detail": "d"}}),
        (PredictionStatus.CANCELLED, {}),
        (PredictionStatus.IN_PROGRESS, {"attempt": 2}),
    ],
)
def test_get_shows_each_status_with_200(env, status, fields):
    posted = accept(env).json()
    (row,) = env.conn.preds.values()
    row.update(status=status, **fields)
    r = env.get(posted["id"])
    assert r.status_code == 200 and r.json()["status"] == status
    assert r.json()["output"] == fields.get("output") and r.json()["error"] == fields.get("error") and r.json()["usage"]["attempt"] == row["attempt"]


def test_get_after_the_retention_purge_says_expired_and_has_no_output(env):
    posted = accept(env).json()
    (row,) = env.conn.preds.values()
    row.update(status=PredictionStatus.COMPLETED, output=None, input=None, purged_at=datetime.now(timezone.utc))
    r = env.get(posted["id"])
    assert r.status_code == 200 and r.json()["expired"] is True and r.json()["output"] is None and r.json()["status"] == "completed"


def test_another_key_of_the_same_project_gets_the_one_404_for_a_prediction_it_did_not_make(env):
    posted = accept(env).json()
    other = env.add_key()
    mine, theirs, unknown, malformed = env.get(posted["id"]), env.get(posted["id"], secret=other), env.get(f"pred_{uuid.uuid4()}", secret=other), env.get("nonsense", secret=other)
    assert mine.status_code == 200
    assert theirs.status_code == unknown.status_code == malformed.status_code == 404
    strip = lambda r: {k: v for k, v in r.json().items() if k != "request_id"}  # noqa: E731
    assert strip(theirs) == strip(unknown) == strip(malformed) and theirs.json()["code"] == "not_found"


def test_a_key_of_another_project_is_a_401_before_any_lookup(env):
    posted = accept(env).json()
    project = uuid.uuid4()
    assert env.get(posted["id"], secret=env.add_key(project=project), project=project).status_code == 404  # its own project, not the prediction's
    assert env.get(posted["id"], project=project).status_code == 401  # the header does not match the key's project


def test_get_needs_a_valid_key(env):
    posted = accept(env).json()
    assert env.get(posted["id"], secret="drl_nope").status_code == 401
