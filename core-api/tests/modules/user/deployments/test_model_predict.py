"""POST /v1/predict: every status of the error table through the real route and require_key, database and Serve faked."""
import uuid

import pytest

from app.modules.user.deployments.constants import COLD_START_RETRY_AFTER_SECONDS

from app.core.authz import TenantStatus
from app.integrations.serve_client import ServeErrorCode
from app.modules.user.deployments import routes, shared
from app.modules.user.deployments.constants import METADATA_MAX_BYTES, PREDICTION_ID_PREFIX
from app.modules.user.deployments.status import DeploymentStatus
from tests.support.predict_env import BODY, MODEL, PROJECT, RESOLVED, TENANT, DEPLOYMENT, RUN, build

@pytest.fixture
def env(monkeypatch):
    return build(monkeypatch)


def test_200_returns_the_envelope_and_forwards_input_unchanged(env):
    r = env.post()
    body = r.json()
    assert r.status_code == 200
    assert body["id"].startswith(PREDICTION_ID_PREFIX) and uuid.UUID(body["id"].removeprefix(PREDICTION_ID_PREFIX))
    assert body["model"] == {"id": str(MODEL), "revision": "3"} and body["output"] == {"predictions": [{"label": "cat"}]}
    assert body["usage"] == {"latency_ms": 12.5} and body["metadata"] == {"trace": "t1"} and body["error"] is None and body["created_at"]
    (args,) = env.served
    assert args[:4] == (str(TENANT), PROJECT, DEPLOYMENT, RUN) and args[4] == BODY["input"] and args[5] == 3  # metadata is not forwarded


def test_the_revision_header_reaches_the_lookup_and_none_means_the_default(env):
    env.post(revision="2")
    env.post()
    lookups = [args for sql, args in env.conn.calls if "FROM models m" in sql]
    assert [a[2] for a in lookups] == ["2", None] and all(a[:2] == (MODEL, PROJECT) for a in lookups)


def test_usage_is_recorded_once_per_success_under_the_prediction_id(env):
    body = env.post().json()
    (usage,) = env.usage
    assert usage["source_ref"] == body["id"].removeprefix(PREDICTION_ID_PREFIX) and usage["quantity"] == 1


@pytest.mark.parametrize("body", [{"input": 1, "extra": 2}, {"metadata": {}}, {"input": 1, "metadata": {"k": "x" * METADATA_MAX_BYTES}}])
def test_422_for_an_unknown_key_a_missing_input_or_big_metadata(env, body):
    r = env.post(body)
    assert (r.status_code, r.json()["code"]) == (422, "validation_error") and not env.served and not env.usage


def test_422_for_a_missing_model_header_and_for_a_body_that_is_not_json_object(env):
    assert env.post(model=None).status_code == 422
    assert env.post([1]).status_code == 422


def test_413_over_the_plan_body_limit(env):
    r = env.post({"input": "x" * 300})
    assert (r.status_code, r.json()["code"]) == (413, "body_too_large") and not env.served


def test_401_for_an_unknown_key_and_for_another_project(env):
    assert env.post(secret="drl_nope").status_code == 401
    assert env.post(project=uuid.uuid4()).status_code == 401


def test_403_for_a_model_outside_the_key_allowlist_and_200_inside_it(env):
    assert env.post(secret=env.add_key(models=[uuid.uuid4()])).status_code == 403
    assert env.post(secret=env.add_key(models=[MODEL])).status_code == 200


@pytest.mark.parametrize("model", [uuid.uuid4(), "not-a-uuid"])
def test_404_for_an_unknown_model_and_none_of_it_is_served(env, model):
    env.conn.row = None
    r = env.post(model=model)
    assert (r.status_code, r.json()["code"]) == (404, "not_found") and not env.served and not env.usage


@pytest.mark.parametrize("status", [DeploymentStatus.DEPLOYING, DeploymentStatus.STOPPED, DeploymentStatus.FAILED])
def test_409_when_the_deployment_is_not_active(env, status):
    env.conn.row = {**RESOLVED, "status": status}
    r = env.post()
    assert (r.status_code, r.json()["code"]) == (409, "deployment_not_active") and not env.served and not env.usage


def test_429_with_retry_after_when_rate_limited(env):
    env.limited = True
    r = env.post()
    assert (r.status_code, r.json()["code"]) == (429, "rate_limited") and 1 <= int(r.headers["Retry-After"]) <= 60 and not env.served


def test_503_for_a_suspended_tenant_before_anything_is_read(env):
    r = env.post(secret=env.add_key(tenant_status=TenantStatus.SUSPENDED))
    assert (r.status_code, r.json()["code"]) == (503, "tenant_suspended") and not env.served and not env.conn.calls


@pytest.mark.parametrize(
    ("code", "status", "problem"),
    [
        (ServeErrorCode.BAD_INPUT, 422, "model_rejected_input"),
        (ServeErrorCode.MODEL_LOAD_FAILED, 502, "upstream_error"),
        (ServeErrorCode.UNREACHABLE, 502, "upstream_error"),
        (ServeErrorCode.TIMEOUT, 504, "upstream_timeout"),
    ],
)
def test_serve_failures_map_and_cost_nothing(env, code, status, problem):
    def fail(*a):
        raise shared.ServeFailed(code, "secret internals " + "x" * 600, 3.0)

    env.answer = fail
    r = env.post()
    assert (r.status_code, r.json()["code"]) == (status, problem) and not env.usage
    assert len(r.json()["detail"]) <= 500 and "secret internals" not in r.json()["detail"] * (problem != "model_rejected_input")
    assert r.headers.get("Retry-After") == (str(COLD_START_RETRY_AFTER_SECONDS) if status == 504 else None)


def test_the_legacy_endpoint_is_still_there():
    assert {"/v1/predict", "/v1/predict/{deployment_id}"} <= {r.path for r in routes.router.routes}
