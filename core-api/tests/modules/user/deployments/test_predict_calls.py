"""predict_calls: the enum mirrors the table's CHECK, key-authenticated shared calls are logged (not bodies), the log never fails a call."""
import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.core import plan_limits
from app.integrations import serve_client
from app.integrations.serve_client import ServeErrorCode
from app.modules.user.deployments import calls, predict
from app.modules.user.deployments.status import DeploymentStatus
from tests.support import FakeConn
from tests.support.check_mirror import assert_mirrors
from tests.support.counted_conn import patch_counted_conn

TENANT, PROJECT, RUN, DEPLOYMENT = str(uuid.uuid4()), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def test_the_error_codes_mirror_the_check():
    assert_mirrors("predict_calls", "error_code", ServeErrorCode)


@pytest.fixture
def predicting(monkeypatch):
    """predict.predict: the key lookup, rate limit, tenant reads, model lookup, Serve call and usage are faked; `log` is the predict_calls conn."""
    lookup = FakeConn(row={"route_prefix": "/p", "status": DeploymentStatus.ACTIVE, "rate_limit_per_minute": 60, "tenant_id": TENANT, "project_id": PROJECT, "tenant_status": "active"})

    async def check(*_):
        return True

    async def record_usage(*_, **__):
        return None

    log = FakeConn()
    reads = FakeConn(val=RUN)
    patch_counted_conn(monkeypatch, reads, predict)
    patch_counted_conn(monkeypatch, log, calls)
    monkeypatch.setattr(predict, "get_pool", lambda: _pool(lookup))
    monkeypatch.setattr(predict.rate_limit, "check", check)
    monkeypatch.setattr(predict, "record_usage", record_usage)
    state = SimpleNamespace(log=log, answer=None)

    async def target(tenant_id, project_id, deployment_id, run_id):
        return predict.Target(serve_client.ComputeTarget.CPU, run_id, None)

    async def serve(*args):
        return state.answer(*args)

    async def predict_limits(conn, tenant_id):
        return plan_limits.Predict(max_body_bytes=1000, max_inflight_per_deployment=3)

    monkeypatch.setattr(predict.plan_limits, "predict_limits", predict_limits)
    monkeypatch.setattr(predict, "_target", target)
    monkeypatch.setattr(predict.serve_client, "predict", serve)
    return state


async def _pool(conn):
    class Acquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *exc):
            return False

    return SimpleNamespace(acquire=lambda: Acquire())


def logged(state):
    return [args for sql, args in state.log.calls if sql.startswith("INSERT INTO predict_calls")]


def test_a_successful_shared_call_is_logged_with_its_run_latency_and_cold_load(predicting):
    predicting.answer = lambda *a: serve_client.Prediction(predictions=[1], cold_load=True)
    assert asyncio.run(predict.predict(DEPLOYMENT, "key", {"text": "x"})) == {"predictions": [1]}
    (args,) = logged(predicting)
    assert args[:4] == (TENANT, DEPLOYMENT, RUN, 200) and args[4] >= 0 and args[5:7] == (True, None) and args[7] == len('{"text": "x"}')


def test_a_failed_shared_call_is_logged_with_its_code_and_still_raises(predicting):
    def fail(*a):
        raise serve_client.ServeCallError(ServeErrorCode.MODEL_LOAD_FAILED, "boom")

    predicting.answer = fail
    with pytest.raises(predict.ServeFailed):
        asyncio.run(predict.predict(DEPLOYMENT, "key", {}))
    (args,) = logged(predicting)
    assert args[2:4] == (RUN, 502) and args[5:8] == (None, ServeErrorCode.MODEL_LOAD_FAILED, len("{}"))


def test_a_log_that_cannot_be_written_does_not_fail_the_call(monkeypatch):
    class Broken:
        async def __aenter__(self):
            raise OSError("db down")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(calls, "TenantConn", lambda tenant_id: Broken())
    asyncio.run(calls.record(TENANT, DEPLOYMENT, RUN, status=200, latency_ms=1, cold_load=False, error_code=None, request_bytes=2))


def test_the_purge_asks_for_the_configured_retention(monkeypatch):
    conn = FakeConn(val=0)

    async def get_pool():
        return await _pool(conn)

    monkeypatch.setattr(calls, "get_pool", get_pool)
    asyncio.run(calls.purge_predict_calls())
    (sql, args), = conn.calls
    assert "purge_predict_calls" in sql and args == (calls.settings.serving.predict_calls_retention_days,)
