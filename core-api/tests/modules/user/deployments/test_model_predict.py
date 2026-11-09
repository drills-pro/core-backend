"""POST /v1/predict: every status of the error table through the real route and require_key, database and Serve faked."""
import uuid
from types import SimpleNamespace

import pytest

from app.core import plan_limits
from app.core.authz import TenantStatus, generate_key, keys
from app.integrations import serve_client
from app.integrations.serve_client import ServeErrorCode
from app.modules.user.deployments import calls, model_predict, routes, shared
from app.modules.user.deployments.constants import METADATA_MAX_BYTES, PREDICTION_ID_PREFIX, Serving
from app.modules.user.deployments.status import DeploymentStatus
from tests.support import FakeConn, make_client
from tests.support.counted_conn import patch_counted_conn

TENANT, PROJECT, MODEL, RUN, DEPLOYMENT = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
BODY = {"input": {"text": "hello", "n": 1.0}, "metadata": {"trace": "t1"}}
RESOLVED = {
    "id": DEPLOYMENT, "status": DeploymentStatus.ACTIVE, "serving": Serving.SHARED, "route_prefix": "/p", "training_run_id": RUN,
    "rate_limit_per_minute": 60, "mlflow_version": "3",
}


class Pool:
    def __init__(self):
        self.rows = {}

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetchrow(self, sql, key_hash):
        return self.rows.get(key_hash)


@pytest.fixture
def env(monkeypatch):
    pool = Pool()

    async def get_pool():
        return pool

    monkeypatch.setattr(keys, "get_pool", get_pool)
    conn = FakeConn(row=RESOLVED)
    patch_counted_conn(monkeypatch, conn, model_predict, calls)
    state = SimpleNamespace(conn=conn, pool=pool, served=[], usage=[], limited=False, answer=None, key=None)

    def add_key(models=(), tenant_status=TenantStatus.ACTIVE, project=PROJECT):
        key = generate_key()
        pool.rows[key.key_hash] = dict(
            key_id=uuid.uuid4(), tenant_id=TENANT, project_id=project, scopes=["predict"], model_ids=list(models), tenant_status=tenant_status)
        return key.secret

    state.add_key = add_key
    state.key = add_key()

    async def predict_limits(conn, tenant_id):
        return plan_limits.Predict(max_body_bytes=200, max_inflight_per_deployment=3)

    async def check(*_):
        return not state.limited

    async def record_usage(conn, **kw):
        state.usage.append(kw)

    async def answer(*args):
        state.served.append(args)
        return state.answer(*args)

    state.answer = lambda *a: (serve_client.Prediction(predictions=[{"label": "cat"}], cold_load=False), 12.5)
    monkeypatch.setattr(model_predict.plan_limits, "predict_limits", predict_limits)
    monkeypatch.setattr(model_predict.rate_limit, "check", check)
    monkeypatch.setattr(model_predict, "record_usage", record_usage)
    monkeypatch.setattr(model_predict.shared, "answer", answer)
    client = make_client(routes.router, headers=None)

    def post(body=BODY, *, secret=None, model=MODEL, revision=None, project=PROJECT, **kw):
        headers = {"Authorization": f"Bearer {secret or state.key}", "X-Project-Id": str(project)}
        if model is not None:
            headers["X-Model-Id"] = str(model)
        if revision is not None:
            headers["X-Model-Revision"] = revision
        return client.post("/v1/predict", json=body, headers=headers, **kw)

    state.post = post
    return state


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


def test_the_legacy_endpoint_is_still_there():
    assert {"/v1/predict", "/v1/predict/{deployment_id}"} <= {r.path for r in routes.router.routes}
