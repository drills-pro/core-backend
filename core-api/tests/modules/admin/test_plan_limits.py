"""/v1/admin/plan-limits through the real router, database faked."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import problem
from app.modules.admin.plan_limits import routes
from tests.core.test_plan_limits import groups, row
from tests.support import FakeConn, TenantCtx

URL = "/v1/admin/plan-limits"
BODY = groups()


@pytest.fixture
def call(monkeypatch):
    def run(method, path, conn, **kw):
        async def get_pool():
            return SimpleNamespace(acquire=lambda: TenantCtx(conn))

        monkeypatch.setattr(routes, "get_pool", get_pool)
        app = FastAPI()
        problem.install(app)
        app.dependency_overrides[routes.get_platform_admin] = lambda: "ops"
        app.include_router(routes.router)
        return TestClient(app).request(method, path, **kw)

    return run


def test_put_writes_the_tier_as_the_actor(call):
    conn = FakeConn(row=row())
    res = call("PUT", f"{URL}/pro", conn, json=BODY)
    sql, args = conn.calls[0]
    assert res.status_code == 200 and sql.startswith("INSERT INTO plan_limits")
    assert args == ("pro", *BODY.values(), "ops")


def test_list_returns_every_tier(call):
    res = call("GET", URL, FakeConn(rowset=[row(plan_tier="free"), row()]))
    assert [r["plan_tier"] for r in res.json()] == ["free", "pro"]


@pytest.mark.parametrize("path, bad", [("pro", {"tables": {"csv_max_rows": 0, "csv_max_columns": 1}}), ("pro", {"surprise": 1}), ("gold", {})])
def test_a_bad_request_is_422(call, path, bad):
    assert call("PUT", f"{URL}/{path}", FakeConn(), json={**BODY, **bad}).status_code == 422
