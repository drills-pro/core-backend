"""The admin's plan limits: the service's rules against a connection it is handed (no pool, no HTTP), and the routes' wiring."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import problem
from app.core.enums import AdminAuditAction, AdminAuditEntity, PlanTier
from app.core.plan_limits import PlanLimitsBody
from app.modules.admin.plan_limits import routes, service
from tests.core.test_plan_limits import groups, row
from tests.support import FakeConn, TenantCtx, audits
from tests.support.tx import COMMIT, ROLLBACK, TxConn, statements

URL = "/v1/admin/plan-limits"
BODY = groups()


def test_put_writes_the_tier_as_the_actor_and_audits_it():
    conn = FakeConn(row=row())
    asyncio.run(service.put(conn, PlanTier.PRO, PlanLimitsBody.model_validate(BODY), "ops"))
    sql, args = conn.calls[0]
    assert sql.startswith("INSERT INTO plan_limits") and args == ("pro", *BODY.values(), "ops")
    assert audits(conn) == [("ops", AdminAuditAction.PLAN_LIMITS_PUT, AdminAuditEntity.PLAN_LIMITS, "pro", BODY)]


def test_the_write_and_its_audit_row_share_one_transaction_and_fail_together():
    conn = TxConn(row=row())
    asyncio.run(service.put(conn, PlanTier.PRO, PlanLimitsBody.model_validate(BODY), "ops"))
    assert statements(conn)[-2:] == ["INSERT INTO admin_audit", COMMIT]
    failing = TxConn(row=row(), fail_on="INSERT INTO admin_audit")
    with pytest.raises(RuntimeError):
        asyncio.run(service.put(failing, PlanTier.PRO, PlanLimitsBody.model_validate(BODY), "ops"))
    assert statements(failing)[-1] == ROLLBACK


@pytest.fixture
def call(monkeypatch):
    def send(method, path, conn, **kw):
        async def get_pool():
            return SimpleNamespace(acquire=lambda: TenantCtx(conn))

        monkeypatch.setattr(routes, "get_pool", get_pool)
        app = FastAPI()
        problem.install(app)
        app.dependency_overrides[routes.get_platform_admin] = lambda: "ops"
        app.include_router(routes.router)
        return TestClient(app).request(method, path, **kw)

    return send


def test_put_answers_with_the_tier(call):
    res = call("PUT", f"{URL}/pro", FakeConn(row=row()), json=BODY)
    assert res.status_code == 200 and res.json()["plan_tier"] == "pro"


def test_list_returns_every_tier(call):
    res = call("GET", URL, FakeConn(rowset=[row(plan_tier="free"), row()]))
    assert [r["plan_tier"] for r in res.json()] == ["free", "pro"]


@pytest.mark.parametrize("path, bad", [("pro", {"tables": {"csv_max_rows": 0, "csv_max_columns": 1}}), ("pro", {"surprise": 1}), ("gold", {})])
def test_a_bad_request_is_422(call, path, bad):
    assert call("PUT", f"{URL}/{path}", FakeConn(), json={**BODY, **bad}).status_code == 422
