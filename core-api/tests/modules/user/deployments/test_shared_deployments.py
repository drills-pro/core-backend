"""Shared deployments: created active with no Ray job, answered by the predictor apps under their run, capped and mapped to statuses."""
import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import plan_limits
from app.core.problem import Problem
from app.core.enums import TaskType
from app.integrations import serve_client
from app.integrations.serve_client import ServeCallError, ServeErrorCode
from app.modules.user.deployments import predict, service
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
SERVABLE = {"id": RUN, "status": TrainingStatus.SUCCEEDED, "mlflow_run_id": "mlflow-1", "class_names": ["cat", "dog"], "test_dataset_id": uuid.uuid4()}


@pytest.fixture(autouse=True)
def fresh_state():
    predict._targets.clear()
    predict._inflight.clear()


@pytest.fixture
def shared_env(monkeypatch):
    conn = FakeConn(
        rows={"idempotency_key = $2": None, "FROM training_runs": SERVABLE, "INSERT INTO deployments": None},
        vals={"count(*) FROM deployments": 0, "SELECT settings": {}, "SELECT plan_tier": "pro", "SELECT task_type": TaskType.IMAGE_CLASSIFICATION})
    patch_counted_conn(monkeypatch, conn, service)
    return conn


def inserted(conn):
    return next(args for sql, args in conn.calls if sql.startswith("INSERT INTO deployments"))


def test_a_shared_deployment_is_active_at_once_with_no_ray_job(shared_env):
    shared_env.rows["INSERT INTO deployments"] = {"id": uuid.uuid4(), "status": "active", "route_prefix": "/predict/x", "rate_limit_per_minute": 120}
    out = asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
    args = inserted(shared_env)
    assert args[4] is None and args[8:] == (Serving.SHARED, "active", KEY, False) and out.status == "active" and out.predict_key


def test_a_main_redeploy_moves_the_row_to_the_new_run_as_shared(shared_env):
    main_id = uuid.uuid4()
    shared_env.vals["SELECT settings"] = {SETTING_DEPLOY_STRATEGY: DeployStrategy.MAIN, SETTING_COMPUTE_TARGET: "gpu"}
    shared_env.rows["FROM deployments"] = {"id": main_id, "status": "active"}
    shared_env.rows["UPDATE deployments"] = {
        "id": main_id, "tenant_id": CTX.tenant_id, "project_id": PROJECT, "status": "active", "route_prefix": "/predict/p/main", "rate_limit_per_minute": 10}
    out = asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
    sql, args = next(c for c in shared_env.calls if c[0].startswith("UPDATE deployments"))
    assert "serving = $6" in sql and args[:8] == (main_id, "active", "active", RUN, None, Serving.SHARED, KEY, False) and out.predict_key is None


@pytest.fixture
def served(monkeypatch):
    """A deployment whose run, task and settings are in the fake database and whose Serve call is recorded."""
    conn = FakeConn(
        rows={"FROM training_runs": SERVABLE},
        vals={"SELECT settings": {SETTING_COMPUTE_TARGET: "gpu"}, "SELECT task_type": TaskType.IMAGE_CLASSIFICATION, "FROM deployments": RUN})
    opened = patch_counted_conn(monkeypatch, conn, predict)
    state = SimpleNamespace(calls=[], urls=0, error=None, conn=conn, max_inflight=8)

    async def predict_limits(conn, tenant_id):
        return plan_limits.Predict(max_body_bytes=1000, max_inflight_per_deployment=state.max_inflight)

    async def served_model(task, mlflow_run_id, class_names, tenant_id, project_id):
        state.urls += 1
        return MODEL

    async def predict_(app, deployment_id, run_id, model, body):
        assert opened.now == 0, "Serve called while a connection was held"
        state.calls.append((app, deployment_id, run_id, body))
        if hasattr(state, "gate"):
            await state.gate.wait()
        if state.error:
            raise state.error
        return serve_client.Prediction(predictions=[{"class": "cat"}], cold_load=False)

    monkeypatch.setattr(predict.serve_client, "served_model", served_model)
    monkeypatch.setattr(predict.serve_client, "predict", predict_)
    monkeypatch.setattr(predict.plan_limits, "predict_limits", predict_limits)
    return state


def answer(body=None):
    return asyncio.run(predict.answer(CTX.tenant_id, PROJECT, DEPLOYMENT, body or {"text": "x"}))


def test_a_request_goes_to_the_projects_compute_target_under_the_deployments_run(served):
    done = answer()
    assert served.calls == [(serve_client.ComputeTarget.GPU, str(DEPLOYMENT), str(RUN), {"text": "x"})]
    assert done.prediction.predictions == [{"class": "cat"}] and done.elapsed_ms >= 0
    assert (done.run_id, done.request_bytes) == (RUN, len('{"text": "x"}'))


def test_the_model_urls_are_looked_up_once_per_run_and_a_new_run_looks_them_up_again(served):
    answer()
    answer()
    assert served.urls == 1
    served.conn.vals["FROM deployments"] = uuid.uuid4()  # a 'main' re-deploy moved the deployment to another run
    answer()
    assert served.urls == 2


def test_the_target_cache_drops_the_least_recently_used_pair_past_its_size(served, monkeypatch):
    monkeypatch.setattr(predict, "TARGET_CACHE_SIZE", 2)
    a, b, c = (uuid.uuid4() for _ in range(3))

    def answer_with(run):  # the deployment serves `run` now (a 'main' re-deploy moved it)
        served.conn.vals["FROM deployments"] = run
        answer()

    for run in (a, b):
        answer_with(run)
    answer_with(a)  # a is now the newest, so b is the oldest
    answer_with(c)
    assert set(predict._targets) == {(DEPLOYMENT, a), (DEPLOYMENT, c)} and served.urls == 3
    answer_with(b)  # evicted: its model URLs are looked up again
    assert served.urls == 4


def test_a_body_over_the_plans_cap_is_413_before_serve_is_called(served):
    with pytest.raises(HTTPException) as err:
        answer({"text": "x" * 2000})
    assert err.value.status_code == 413 and not served.calls


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

    served.max_inflight = 1

    async def run():
        served.gate = asyncio.Event()
        first = asyncio.create_task(predict.answer(CTX.tenant_id, PROJECT, DEPLOYMENT, {}))
        while not served.calls:  # the first request holds its slot inside the Serve call
            await asyncio.sleep(0)
        with pytest.raises(HTTPException) as err:
            await predict.answer(CTX.tenant_id, PROJECT, DEPLOYMENT, {})
        assert err.value.status_code == 429
        served.gate.set()
        await first
        await predict.answer(CTX.tenant_id, PROJECT, DEPLOYMENT, {})  # the slot came back

    asyncio.run(run())
    assert not predict._inflight


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


def test_the_test_call_answers_with_the_predictions_and_logs_the_call(monkeypatch):
    seen = []

    async def answer_(tenant_id, project_id, deployment_id, body):
        seen.append((tenant_id, deployment_id, body))
        return predict.Answered(run_id=RUN, request_bytes=1, prediction=serve_client.Prediction(predictions=[1], cold_load=True), elapsed_ms=2.5)

    monkeypatch.setattr(service.predict, "answer", answer_)
    conn = FakeConn(row={"status": "active"})
    patch_counted_conn(monkeypatch, conn, service)
    out = asyncio.run(service.test_deployment(CTX, PROJECT, DEPLOYMENT, {"input": {"text": "x"}}))
    assert out.response == {"predictions": [1]} and out.latency_ms == 2.5 and seen == [(CTX.tenant_id, DEPLOYMENT, {"text": "x"})]
    assert any(sql.startswith("INSERT INTO deployment_test_calls") for sql, _ in conn.calls)


def test_a_try_it_body_without_an_input_object_is_refused_before_the_model(monkeypatch):
    async def answer_(*args):
        raise AssertionError("the model must not be called")

    monkeypatch.setattr(service.predict, "answer", answer_)
    conn = FakeConn(row={"status": "active"})
    patch_counted_conn(monkeypatch, conn, service)
    with pytest.raises(Problem) as err:
        asyncio.run(service.test_deployment(CTX, PROJECT, DEPLOYMENT, {"text": "x"}))
    assert err.value.status_code == 422
