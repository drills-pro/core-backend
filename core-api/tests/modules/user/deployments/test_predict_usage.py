"""Usage (planning/24): a prediction is metered only when the caller got a 2xx, once per prediction id. Replays, retries, failures and every
refused request produce no usage event."""
import uuid

import pytest

from app.core.authz import TenantStatus
from app.integrations.serve_client import ServeErrorCode
from app.modules.user.deployments.predict import ServeFailed
from app.modules.user.deployments.status import DeploymentStatus
from app.modules.user.usage import UsageResource, UsageUnit
from tests.support.predict_env import RESOLVED, build

KEY = "usage-key"


@pytest.fixture
def env(monkeypatch):
    return build(monkeypatch)


def serve_fails(env, code=ServeErrorCode.MODEL_LOAD_FAILED):
    def fail(*a):
        raise ServeFailed(code, "x", 1.0, None, 0)

    env.answer = fail


def test_a_200_records_exactly_one_event_under_the_prediction_id(env):
    body = env.post().json()
    (event,) = env.usage
    assert event["source_ref"] == body["id"].removeprefix("pred_") and (event["quantity"], event["unit"], event["resource_type"]) == (1, UsageUnit.REQUESTS, UsageResource.INFERENCE)


def test_a_stored_200_records_one_event_under_its_row_id_and_a_replay_adds_none(env):
    first = env.post(key=KEY).json()
    for _ in range(3):
        assert env.post(key=KEY).json() == first
    (event,) = env.usage
    assert event["source_ref"] == first["id"].removeprefix("pred_") and len(env.served) == 1


@pytest.mark.parametrize("key", [None, KEY])
@pytest.mark.parametrize("code", [ServeErrorCode.BAD_INPUT, ServeErrorCode.TIMEOUT, ServeErrorCode.UNREACHABLE, ServeErrorCode.MODEL_LOAD_FAILED])
def test_a_failed_serve_call_records_nothing_and_neither_does_its_replay(env, key, code):
    serve_fails(env, code)
    for _ in range(2):
        assert env.post(key=key).status_code >= 400
    assert not env.usage


def unknown_model(env):
    env.conn.row = None
    return env.post(model=uuid.uuid4())


@pytest.mark.parametrize(
    "refuse",
    [
        lambda env: env.post({"input": 1, "extra": 2}),  # 422
        lambda env: env.post({"input": "x" * 300}),  # 413
        lambda env: env.post(secret="drl_nope"),  # 401
        lambda env: env.post(secret=env.add_key(models=[uuid.uuid4()])),  # 403
        lambda env: env.post(secret=env.add_key(tenant_status=TenantStatus.SUSPENDED)),  # 503
        lambda env: unknown_model(env),  # 404
    ],
)
def test_a_refused_request_records_nothing(env, refuse):
    assert refuse(env).status_code >= 400 and not env.usage


def test_a_deployment_that_is_not_active_and_a_rate_limited_call_record_nothing(env):
    env.conn.row = {**RESOLVED, "status": DeploymentStatus.STOPPED}
    assert env.post().status_code == 409
    env.conn.row = RESOLVED
    env.limited = True
    assert env.post().status_code == 429
    assert not env.usage


def test_a_reused_key_with_another_body_records_nothing_more(env):
    env.post(key=KEY)
    assert env.post({"input": "other"}, key=KEY).status_code == 422
    assert len(env.usage) == 1


def test_two_calls_without_a_key_are_two_predictions_and_two_events(env):
    first, second = env.post().json(), env.post().json()
    assert first["id"] != second["id"] and [e["source_ref"] for e in env.usage] == [first["id"].removeprefix("pred_"), second["id"].removeprefix("pred_")]
