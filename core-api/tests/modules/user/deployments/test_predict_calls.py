"""predict_calls: the enum mirrors the table's CHECK, key-authenticated shared calls are logged (not bodies), the log never fails a call."""
import asyncio
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import plan_limits
from app.integrations import serve_client
from app.integrations.serve_client import ServeErrorCode
from app.modules.user.deployments import calls, service, shared
from app.modules.user.deployments.status import DeploymentStatus
from tests.support import FakeConn
from tests.support.counted_conn import patch_counted_conn

MIGRATION = next((Path(__file__).parents[4] / "migrations").glob("*_predict_calls.sql")).read_text()
TENANT, PROJECT, RUN, DEPLOYMENT = str(uuid.uuid4()), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def test_the_error_codes_mirror_the_check():
    listed = re.search(r"error_code IN \(([^)]*)\)", MIGRATION).group(1).replace("'", "").replace(" ", "").replace("\n", "").split(",")
    assert set(listed) == {c.value for c in ServeErrorCode}


@pytest.fixture
def predicting(monkeypatch):
    """service.predict for a shared deployment: the key lookup, rate limit, tenant reads and usage are faked; `log` is the predict_calls conn."""
    lookup = FakeConn(row={"route_prefix": "/p", "status": DeploymentStatus.ACTIVE, "rate_limit_per_minute": 60, "tenant_id": TENANT, "project_id": PROJECT, "tenant_status": "active"})

    async def check(*_):
        return True

    async def record_usage(*_, **__):
        return None

    log = FakeConn()
    reads = FakeConn(row={"serving": "shared", "training_run_id": RUN})
    patch_counted_conn(monkeypatch, reads, service)
    patch_counted_conn(monkeypatch, log, calls)
    monkeypatch.setattr(service, "get_pool", lambda: _pool(lookup))
    monkeypatch.setattr(service.rate_limit, "check", check)
    monkeypatch.setattr(service, "record_usage", record_usage)
    state = SimpleNamespace(log=log, answer=None)

    async def answer(*args):
        return state.answer(*args)

    async def predict_limits(conn, tenant_id):
        return plan_limits.Predict(max_body_bytes=1000, max_inflight_per_deployment=3)

    monkeypatch.setattr(service.plan_limits, "predict_limits", predict_limits)

    monkeypatch.setattr(service.shared, "answer", answer)
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
    predicting.answer = lambda *a: (serve_client.Prediction(predictions=[1], cold_load=True), 12.5)
    assert asyncio.run(service.predict(DEPLOYMENT, "key", {"text": "x"})) == {"predictions": [1]}
    (args,) = logged(predicting)
    assert args[:5] == (TENANT, DEPLOYMENT, RUN, 200, 12.5) and args[5:7] == (True, None) and args[7] == len('{"text": "x"}')


def test_a_failed_shared_call_is_logged_with_its_code_and_still_raises(predicting):
    def fail(*a):
        raise shared.ServeFailed(ServeErrorCode.MODEL_LOAD_FAILED, "boom", 3.0)

    predicting.answer = fail
    with pytest.raises(shared.ServeFailed):
        asyncio.run(service.predict(DEPLOYMENT, "key", {}))
    (args,) = logged(predicting)
    assert args[3:7] == (502, 3.0, None, ServeErrorCode.MODEL_LOAD_FAILED)


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
