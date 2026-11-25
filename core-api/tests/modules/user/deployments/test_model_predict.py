"""POST /v1/predict: every status of the error table through the real route and require_key, database and Serve faked."""
import uuid

import pytest

from app.modules.user.deployments.constants import COLD_START_RETRY_AFTER_SECONDS

from app.core.authz import TenantStatus
from app.integrations.serve_client import ServeErrorCode
from app.modules.user.deployments import model_predict, routes
from app.modules.user.deployments.predict import ServeFailed
from app.modules.user.deployments.constants import METADATA_MAX_BYTES, PREDICTION_ID_PREFIX
from app.modules.user.deployments.status import DeploymentStatus
from tests.support.predict_env import BODY, PROJECT, RESOLVED, TENANT, DEPLOYMENT, RUN, build

@pytest.fixture
def env(monkeypatch):
    return build(monkeypatch)


def test_200_returns_the_envelope_and_forwards_input_unchanged(env):
    r = env.post()
    body = r.json()
    assert r.status_code == 200
    assert body["id"].startswith(PREDICTION_ID_PREFIX) and uuid.UUID(body["id"].removeprefix(PREDICTION_ID_PREFIX))
    assert body["model"] == {"id": str(DEPLOYMENT)} and body["output"] == {"predictions": [{"label": "cat"}]}
    assert body["usage"] == {"latency_ms": 12.5} and body["metadata"] == {"trace": "t1"} and body["error"] is None and body["created_at"]
    (args,) = env.served
    assert args[:4] == (str(TENANT), PROJECT, DEPLOYMENT, RUN) and args[4] == BODY["input"] and args[5] == 3  # metadata is not forwarded


def test_no_model_header_looks_up_the_projects_main_deployment_and_a_model_id_looks_up_that_one(env):
    assert env.post(model=None).status_code == 200
    assert env.post(model=DEPLOYMENT).status_code == 200
    lookups = [(sql, args) for sql, args in env.conn.calls if sql.startswith("SELECT d.id")]
    assert "d.main" in lookups[0][0] and lookups[0][1][0] == PROJECT
    assert "d.id = $1" in lookups[1][0] and lookups[1][1][0] == DEPLOYMENT


def test_usage_is_recorded_once_per_success_under_the_prediction_id(env):
    body = env.post().json()
    (usage,) = env.usage
    assert usage["source_ref"] == body["id"].removeprefix(PREDICTION_ID_PREFIX) and usage["quantity"] == 1


@pytest.mark.parametrize("body", [{"input": 1, "extra": 2}, {"metadata": {}}, {"input": 1, "metadata": {"k": "x" * METADATA_MAX_BYTES}}])
def test_422_for_an_unknown_key_a_missing_input_or_big_metadata(env, body):
    r = env.post(body)
    assert (r.status_code, r.json()["code"]) == (422, "validation_error") and not env.served and not env.usage


def test_422_for_a_body_that_is_not_json_object(env):
    assert env.post([1]).status_code == 422


def test_413_over_the_plan_body_limit(env):
    r = env.post({"input": "x" * 300})
    assert (r.status_code, r.json()["code"]) == (413, "body_too_large") and not env.served


def test_401_for_an_unknown_key_and_for_another_project(env):
    assert env.post(secret="drl_nope").status_code == 401
    assert env.post(project=uuid.uuid4()).status_code == 401


def test_403_for_a_model_outside_the_key_allowlist_and_200_inside_it(env):
    assert env.post(secret=env.add_key(models=[uuid.uuid4()])).status_code == 403
    assert env.post(secret=env.add_key(models=[DEPLOYMENT])).status_code == 200


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
        raise ServeFailed(code, "secret internals " + "x" * 600, 3.0, None, 0)

    env.answer = fail
    r = env.post()
    assert (r.status_code, r.json()["code"]) == (status, problem) and not env.usage
    assert len(r.json()["detail"]) <= 500 and "secret internals" not in r.json()["detail"] * (problem != "model_rejected_input")
    assert r.headers.get("Retry-After") == (str(COLD_START_RETRY_AFTER_SECONDS) if status == 504 else None)


def test_the_legacy_endpoint_is_gone_and_predict_has_no_revision_header():
    assert "/v1/predict/{deployment_id}" not in {r.path for r in routes.router.routes}


@pytest.mark.parametrize("code", list(ServeErrorCode))
def test_serve_problem_maps_every_code_and_only_the_models_refusal_shows_serves_words(code):
    problem = model_predict.serve_problem(ServeFailed(code, "internal detail", 1.0, None, 0))
    assert problem.status_code == {ServeErrorCode.BAD_INPUT: 422, ServeErrorCode.TIMEOUT: 504}.get(code, 502)  # unreachable is 502 here, 503 only on the per-deployment-key route
    assert ("internal detail" in problem.detail) == (code == ServeErrorCode.BAD_INPUT)
