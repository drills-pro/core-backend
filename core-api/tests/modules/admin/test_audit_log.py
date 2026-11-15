"""GET /v1/admin/audit-log through the real router, database faked."""
import datetime
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import problem
from app.core.enums import AdminAuditAction, AdminAuditEntity
from app.modules.admin.audit_log import routes, service
from tests.support import FakeConn, TenantCtx

URL = "/v1/admin/audit-log"
NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


def entry(total=2, **over):
    return dict(id=uuid.uuid4(), actor="ops", action=AdminAuditAction.PLAN_LIMITS_PUT, entity_type=AdminAuditEntity.PLAN_LIMITS,
                entity_id="pro", detail={"k": 1}, created_at=NOW, total=total) | over


@pytest.fixture
def call(monkeypatch):
    def run(conn, **kw):
        async def get_pool():
            return SimpleNamespace(acquire=lambda: TenantCtx(conn))

        monkeypatch.setattr(service, "get_pool", get_pool)
        app = FastAPI()
        problem.install(app)
        app.dependency_overrides[routes.get_platform_admin] = lambda: "ops"
        app.include_router(routes.router)
        return TestClient(app).get(URL, **kw)

    return run


def test_lists_the_trail_with_the_filtered_total(call):
    conn = FakeConn(rowset=[entry(), entry(entity_type=AdminAuditEntity.TENANT)])
    res = call(conn)
    assert res.status_code == 200 and res.headers["X-Total-Count"] == "2"
    assert [(r["actor"], r["action"], r["entity_id"], r["detail"]) for r in res.json()][0] == ("ops", "plan_limits.put", "pro", {"k": 1})
    assert conn.calls[0][1] == (None, None, 50, 0)


def test_filters_and_pages(call):
    conn = FakeConn(rowset=[entry(total=7)])
    res = call(conn, params={"entity_type": "tenant", "actor": "ops-2", "limit": 1, "offset": 3})
    assert res.headers["X-Total-Count"] == "7" and conn.calls[0][1] == (AdminAuditEntity.TENANT, "ops-2", 1, 3)


def test_an_empty_page_has_a_zero_total(call):
    res = call(FakeConn())
    assert res.json() == [] and res.headers["X-Total-Count"] == "0"


@pytest.mark.parametrize("params", [{"entity_type": "dataset"}, {"limit": 0}, {"limit": 201}, {"offset": -1}])
def test_a_bad_query_is_422(call, params):
    assert call(FakeConn(), params=params).status_code == 422


def test_it_needs_the_platform_admin(dev_headers):
    app = FastAPI()
    problem.install(app)
    app.include_router(routes.router)
    assert TestClient(app).get(URL).status_code == 401
