"""Try-it log: a filtered list of stored predictions (source=console) with a bounded limit; the old log table is still purged."""
import asyncio
import re
import uuid
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.core.authz import CallSource
from app.modules.user.deployments import calls, routes, service
from app.modules.user.deployments.constants import (
    TEST_CALLS_DEFAULT_LIMIT,
    TEST_CALLS_MAX_LIMIT,
    TEST_CALLS_MIN_LIMIT,
    PredictionStatus,
)
from tests.support import PROJECT, FakeConn, access, make_client, patch_tenant_conn

MIGRATION = next((Path(__file__).parents[4] / "migrations").glob("*_test_calls_purge.sql")).read_text()
DEPLOYMENT = uuid.uuid4()
URL = f"/v1/projects/{PROJECT}/deployments/{DEPLOYMENT}/test-calls"


@pytest.fixture
def listing(monkeypatch, dev_headers):
    client = make_client(routes.router, raise_server_exceptions=False)
    conn = FakeConn(access(), val=1)
    patch_tenant_conn(monkeypatch, conn, service)
    return lambda query: (client.get(URL + query), conn)


def test_the_limit_is_bounded_and_defaults(listing):
    for bad in (TEST_CALLS_MIN_LIMIT - 1, TEST_CALLS_MAX_LIMIT + 1, -5):
        assert listing(f"?limit={bad}")[0].status_code == 422
    for good in (TEST_CALLS_MIN_LIMIT, TEST_CALLS_MAX_LIMIT):
        assert listing(f"?limit={good}")[0].status_code == 200
    r, conn = listing("")
    assert r.status_code == 200 and conn.calls[-1][1][-1] == TEST_CALLS_DEFAULT_LIMIT


def _prediction(**kw):
    return {"id": uuid.uuid4(), "status": PredictionStatus.COMPLETED, "input": {"x": 1}, "input_ref": None, "output": {"predictions": [1]},
            "output_ref": None, "error": None, "latency_ms": 4.0, "purged_at": None, "created_at": datetime.now(timezone.utc), **kw}


def test_the_log_lists_the_console_predictions_of_the_deployment(listing):
    listing_conn = listing("")[1]
    listing_conn.rowset = [_prediction(), _prediction(status=PredictionStatus.FAILED, output=None, error={"code": "upstream_error", "detail": "x"})]
    r, conn = listing("")
    ok, failed = r.json()
    assert ok["id"].startswith("pred_") and ok["request"] == {"input": {"x": 1}} and ok["response"] == {"predictions": [1]}
    assert failed["status"] == "failed" and failed["error"]["code"] == "upstream_error" and failed["response"] is None
    sql, args = conn.calls[-1]
    assert "FROM predictions" in sql and args == (DEPLOYMENT, CallSource.CONSOLE, TEST_CALLS_DEFAULT_LIMIT)  # only what the console made


def test_a_purged_prediction_shows_no_payload(listing):
    conn = listing("")[1]
    conn.rowset = [_prediction(purged_at=datetime.now(timezone.utc), input=None, output=None)]
    (call,) = listing("")[0].json()
    assert call["request"] is None and call["response"] is None


def test_the_log_of_another_deployment_is_404(listing):
    conn = listing("")[1]
    conn.vals["FROM deployments"] = None
    assert listing("")[0].status_code == 404


def test_the_purge_asks_for_the_configured_retention(monkeypatch):
    conn = FakeConn(val=0)

    async def get_pool():
        return SimpleNamespace(acquire=lambda: _Acquire(conn))

    monkeypatch.setattr(calls, "get_pool", get_pool)
    asyncio.run(calls.purge_test_calls())
    (sql, args), = conn.calls
    assert "purge_test_calls" in sql and args == (calls.settings.serving.test_calls_retention_days,)


def test_the_purge_is_polled_and_the_function_is_granted():
    from app import background

    assert any(fn is calls.purge_test_calls for _, fn, _ in background.POLLERS)
    assert re.search(r"GRANT EXECUTE ON FUNCTION purge_test_calls\(integer\) TO core_app", MIGRATION)
    assert "SECURITY DEFINER SET search_path" in MIGRATION


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False
