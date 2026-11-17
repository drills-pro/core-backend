"""Try-it log: the list limit is bounded, big payloads are replaced by a marker, old rows are purged."""
import asyncio
import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.integrations import serve_client
from app.modules.user.deployments import calls, routes, service
from app.modules.user.deployments.constants import (
    TEST_CALL_STORED_MAX_BYTES,
    TEST_CALLS_DEFAULT_LIMIT,
    TEST_CALLS_MAX_LIMIT,
    TEST_CALLS_MIN_LIMIT,
    TRUNCATED_BYTES_FIELD,
    TRUNCATED_FIELD,
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


def test_a_small_payload_is_kept_and_a_big_one_becomes_a_marker():
    small = {"text": "x"}
    assert service._stored(small) == small
    big = {"text": "x" * TEST_CALL_STORED_MAX_BYTES}
    assert service._stored(big) == {TRUNCATED_FIELD: True, TRUNCATED_BYTES_FIELD: len(json.dumps(big))}


def test_test_deployment_logs_the_marker_not_the_big_body(monkeypatch):
    conn = FakeConn(row={"status": "active"})
    patch_tenant_conn(monkeypatch, conn, service)
    big = {"input": {"text": "x" * TEST_CALL_STORED_MAX_BYTES}}

    async def answer(tenant_id, project_id, deployment_id, body):
        prediction = serve_client.Prediction(predictions=[1], cold_load=False)
        return service.predict.Answered(run_id=uuid.uuid4(), request_bytes=len(json.dumps(body)), prediction=prediction, elapsed_ms=1.0)

    monkeypatch.setattr(service.predict, "answer", answer)
    asyncio.run(service.test_deployment(SimpleNamespace(tenant_id=str(uuid.uuid4())), PROJECT, DEPLOYMENT, big))
    (_, _, request, response, _), = conn.executed
    assert request[TRUNCATED_FIELD] is True and response == {"predictions": [1]}


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
