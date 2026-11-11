"""Shared deployments: created active with no Ray job, answered by the predictor apps under their run, capped and mapped to statuses."""
import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import plan_limits
from app.core.enums import TaskType
from app.integrations import serve_client
from app.integrations.serve_client import ServeCallError, ServeErrorCode
from app.modules.user.deployments import service, shared
from app.modules.user.deployments.constants import (
    SERVE_ERROR_STATUS,
    SETTING_COMPUTE_TARGET,
    SETTING_DEPLOY_STRATEGY,
    DeployStrategy,
    Serving,
)
from app.modules.user.training import TrainingStatus
from tests.integrations.test_serve_shared import MODEL
from tests.support import FakeConn
from tests.support.counted_conn import patch_counted_conn

CTX = SimpleNamespace(tenant_id=str(uuid.uuid4()), actor="user-1")
PROJECT, RUN, DEPLOYMENT = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
KEY = "k"
SERVABLE = {"id": RUN, "status": TrainingStatus.SUCCEEDED, "mlflow_run_id": "mlflow-1", "class_names": ["cat", "dog"]}


@pytest.fixture(autouse=True)
def fresh_state():
    shared._targets.clear()
    shared._inflight.clear()


@pytest.fixture
def shared_env(monkeypatch):
    conn = FakeConn(
        rows={"idempotency_key = $2": None, "FROM training_runs": SERVABLE, "INSERT INTO deployments": None},
        vals={"count(*) FROM deployments": 0, "SELECT settings": {}, "SELECT plan_tier": "pro", "SELECT task_type": TaskType.IMAGE_CLASSIFICATION})
    patch_counted_conn(monkeypatch, conn, service)

    async def apps_declared():
        return True

    async def deploy(**kw):
        raise AssertionError("a shared deployment starts no Ray job")

    monkeypatch.setattr(service.serve_client, "apps_declared", apps_declared)
    monkeypatch.setattr(service.serve_client, "deploy", deploy)
    return conn


def inserted(conn):
    return next(args for sql, args in conn.calls if sql.startswith("INSERT INTO deployments"))


def test_a_shared_deployment_is_active_at_once_with_no_ray_job(shared_env):
    shared_env.rows["INSERT INTO deployments"] = {"id": uuid.uuid4(), "status": "active", "route_prefix": "/predict/x", "rate_limit_per_minute": 120}
    out = asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
    args = inserted(shared_env)
    assert args[4] is None and args[8:] == (Serving.SHARED, "active", KEY) and out.status == "active" and out.predict_key


def test_a_main_redeploy_moves_the_row_to_the_new_run_as_shared(shared_env):
    main_id = uuid.uuid4()
    shared_env.vals["SELECT settings"] = {SETTING_DEPLOY_STRATEGY: DeployStrategy.MAIN, SETTING_COMPUTE_TARGET: "gpu"}
    shared_env.rows["FROM deployments"] = {"id": main_id, "status": "active", "predict_key_hash": "h", "rate_limit_per_minute": 10}
    shared_env.rows["UPDATE deployments"] = {"id": main_id, "status": "active", "route_prefix": "/predict/p/main", "rate_limit_per_minute": 10}
    out = asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
    sql, args = next(c for c in shared_env.calls if c[0].startswith("UPDATE deployments"))
    assert "serving = $5" in sql and args == (main_id, RUN, None, "active", Serving.SHARED, "stopped", KEY) and out.predict_key is None


@pytest.fixture
def served(monkeypatch):
    """A deployment whose run, task and settings are in the fake database and whose Serve call is recorded."""
    conn = FakeConn(
        rows={"FROM training_runs": SERVABLE},
        vals={"SELECT settings": {SETTING_COMPUTE_TARGET: "gpu"}, "SELECT task_type": TaskType.IMAGE_CLASSIFICATION})
    opened = patch_counted_conn(monkeypatch, conn, shared)
    state = SimpleNamespace(calls=[], urls=0, error=None, conn=conn)

    async def served_model(task, mlflow_run_id, class_names, tenant_id, project_id):
        state.urls += 1
        return MODEL

    async def predict(app, deployment_id, run_id, model, body):
        assert opened.now == 0, "Serve called while a connection was held"
        state.calls.append((app, deployment_id, run_id, body))
        if hasattr(state, "gate"):
            await state.gate.wait()
        if state.error:
            raise state.error
        return serve_client.Prediction(predictions=[{"class": "cat"}], cold_load=False)

    monkeypatch.setattr(shared.serve_client, "served_model", served_model)
    monkeypatch.setattr(shared.serve_client, "predict", predict)
    return state


def answer(run=RUN, body=None, max_inflight=8):
    return asyncio.run(shared.answer(CTX.tenant_id, PROJECT, DEPLOYMENT, run, body or {"text": "x"}, max_inflight))


def test_a_request_goes_to_the_projects_compute_target_under_the_deployments_run(served):
    prediction, ms = answer()
    assert served.calls == [(serve_client.ComputeTarget.GPU, str(DEPLOYMENT), str(RUN), {"text": "x"})]
    assert prediction.predictions == [{"class": "cat"}] and ms >= 0


def test_the_model_urls_are_looked_up_once_per_run_and_a_new_run_looks_them_up_again(served):
    answer()
    answer()
    assert served.urls == 1
    answer(run=uuid.uuid4())
    assert served.urls == 2


def test_the_target_cache_drops_the_least_recently_used_pair_past_its_size(served, monkeypatch):
    monkeypatch.setattr(shared, "TARGET_CACHE_SIZE", 2)
    a, b, c = (uuid.uuid4() for _ in range(3))
    for run in (a, b):
        answer(run=run)
    answer(run=a)  # a is now the newest, so b is the oldest
    answer(run=c)
    assert set(shared._targets) == {(DEPLOYMENT, a), (DEPLOYMENT, c)} and served.urls == 3
    answer(run=b)  # evicted: its model URLs are looked up again
    assert served.urls == 4


@pytest.mark.parametrize("code", list(ServeErrorCode))
def test_every_serve_error_has_a_status_and_is_raised_with_it(served, code):
    assert code in SERVE_ERROR_STATUS
    served.error = ServeCallError(code, "boom")
    with pytest.raises(HTTPException) as err:
        answer()
    assert err.value.status_code == SERVE_ERROR_STATUS[code] and code.value in err.value.detail


def test_a_run_that_cannot_be_served_is_409(served):
    served.conn.rows["FROM training_runs"] = {**SERVABLE, "class_names": []}
    with pytest.raises(HTTPException) as err:
        answer()
    assert err.value.status_code == 409


def test_requests_over_the_in_flight_cap_get_429_and_free_their_slot(served):

    async def run():
        served.gate = asyncio.Event()
        first = asyncio.create_task(shared.answer(CTX.tenant_id, PROJECT, DEPLOYMENT, RUN, {}, 1))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        with pytest.raises(HTTPException) as err:
            await shared.answer(CTX.tenant_id, PROJECT, DEPLOYMENT, RUN, {}, 1)
        assert err.value.status_code == 429
        served.gate.set()
        await first
        await shared.answer(CTX.tenant_id, PROJECT, DEPLOYMENT, RUN, {}, 1)  # the slot came back

    asyncio.run(run())
    assert not shared._inflight


def test_a_body_over_the_cap_is_413_before_it_is_parsed(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.modules.user.deployments import routes

    monkeypatch.setattr(routes.settings.serving, "max_body_bytes", 10)
    app = FastAPI()

    @app.post("/echo")
    async def echo(body: dict = routes.Depends(routes.capped_body)):
        return body

    client = TestClient(app)
    assert client.post("/echo", json={"a": 1}).status_code == 200
    assert client.post("/echo", json={"a": "x" * 20}).status_code == 413
    assert client.post("/echo", content=b"nope").status_code == 422
    assert client.post("/echo", json=[1]).status_code == 422


def test_the_test_call_and_key_predict_answer_a_shared_deployment_with_its_predictions(monkeypatch):
    seen = []

    async def answer_(tenant_id, project_id, deployment_id, run_id, body, max_inflight):
        seen.append((tenant_id, run_id, body, max_inflight))
        return serve_client.Prediction(predictions=[1], cold_load=True), 2.5

    async def predict_limits(conn, tenant_id):
        return plan_limits.Predict(max_body_bytes=1000, max_inflight_per_deployment=3)

    monkeypatch.setattr(service.shared, "answer", answer_)
    monkeypatch.setattr(service.plan_limits, "predict_limits", predict_limits)
    row = {"status": "active", "route_prefix": "/p", "serving": "shared", "training_run_id": RUN}
    conn = FakeConn(row=row)
    patch_counted_conn(monkeypatch, conn, service)
    out = asyncio.run(service.test_deployment(CTX, PROJECT, DEPLOYMENT, {"text": "x"}))
    assert out.response == {"predictions": [1]} and out.latency_ms == 2.5 and seen == [(CTX.tenant_id, RUN, {"text": "x"}, 3)]
    assert any(sql.startswith("INSERT INTO deployment_test_calls") for sql, _ in conn.calls)
    with pytest.raises(HTTPException) as err:  # the plan's body cap
        asyncio.run(service.test_deployment(CTX, PROJECT, DEPLOYMENT, {"text": "x" * 2000}))
    assert err.value.status_code == 413
