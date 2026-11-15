"""Suspending a tenant flips tenants.status, which require() then enforces; Zitadel is optional."""
import uuid
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core import problem
from app.core.authz import Action, AuthzContext, TenantRole, TenantStatus, require
from app.core.authz.deps import TenantProblem
from app.core.enums import TaskType
from app.modules.admin.tenants import detail, service
from tests.support import FakeConn, TenantCtx, access, patch_tenant_conn
from tests.support.task_types import seeded

TENANT = uuid.uuid4()


class TenantsConn(FakeConn):
    """One tenants row: `state` holds its status; the UPDATE writes it, project_access reads it."""

    def __init__(self, state, org_id):
        super().__init__()
        self.state, self.org_id = state, org_id

    def transaction(self):
        return TenantCtx(None)

    async def fetchrow(self, sql, *args):
        if "UPDATE tenants" in sql:  # (tenant_id, status, the status it may not leave): what tenants.set_status sends
            _, status, kept = args
            previous = self.state["status"]
            if previous != kept:
                self.state["status"] = status
            return {"previous": previous, "org_id": self.org_id or ""}
        if "FROM task_types" in sql:
            return seeded(args[0])
        return access(TenantRole.OWNER, None, None, None, self.state["status"])


@pytest.fixture
def world(monkeypatch, dev_headers):
    calls = []

    async def set_org_state(client, org_id, active):
        calls.append((org_id, active))

    def build(org_id, status=TenantStatus.ACTIVE):
        conn = TenantsConn({"status": status}, org_id)

        async def get_pool():
            return SimpleNamespace(acquire=lambda: TenantCtx(conn))

        monkeypatch.setattr(service, "get_pool", get_pool)
        monkeypatch.setattr(service.zitadel, "set_org_state", set_org_state)
        patch_tenant_conn(monkeypatch, conn)
        app = FastAPI()
        problem.install(app)
        app.dependency_overrides[detail.get_platform_admin] = lambda: None
        app.include_router(detail.router)

        @app.get("/tenant-admin")
        async def gated(ctx: AuthzContext = Depends(require(Action.TENANT_ADMIN))):
            return {}

        return TestClient(app, headers={"X-Tenant-Id": str(TENANT), "X-User-Id": "u1"}), calls

    return build


def _set(client, status):
    return client.post(f"/v1/admin/tenants/{TENANT}/status", json={"status": status})


def test_suspend_then_reactivate_gates_require(world):
    client, calls = world("org-1")
    assert client.get("/tenant-admin").status_code == 200
    assert _set(client, "suspended").json() == {"status": "suspended"}
    r = client.get("/tenant-admin")
    assert (r.status_code, r.json()["code"]) == (403, "tenant_suspended")
    assert _set(client, "active").status_code == 200
    assert client.get("/tenant-admin").status_code == 200
    assert calls == [("org-1", False), ("org-1", True)]


def test_tenant_without_zitadel_org_can_be_suspended(world):
    client, calls = world(None)
    assert _set(client, "suspended").status_code == 200
    assert client.get("/tenant-admin").status_code == 403
    assert calls == []


def test_a_pending_delete_tenant_is_not_moved_back(world):
    client, calls = world("org-1", TenantStatus.PENDING_DELETE)
    for status in (TenantStatus.ACTIVE, TenantStatus.SUSPENDED):
        r = _set(client, status)
        assert (r.status_code, r.json()["code"]) == (409, TenantProblem.PENDING_DELETE)
    r = client.get("/tenant-admin")
    assert (r.status_code, r.json()["code"]) == (403, TenantProblem.PENDING_DELETE)  # still pending; the owner can restore it
    assert calls == []  # Zitadel is left alone too


def test_admin_create_project_rejects_reserved_task_type(world):
    client, _ = world("org-1")
    r = client.post(
        f"/v1/admin/tenants/{TENANT}/projects",
        json={"name": "x", "task_type": TaskType.TEXT_GENERATION},
    )
    assert (r.status_code, r.json()["code"]) == (422, "task_type_not_supported")
