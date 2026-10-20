import uuid
from datetime import datetime, timezone

import pytest

from app.core.authz import TenantRole, TenantStatus
from app.modules.user.tenants import service
from app.modules.user.tenants.constants import ProblemCode
from tests.support import TENANT, FakeConn, access, make_client, patch_tenant_conn

T = f"/v1/tenants/{TENANT}"
NAME = "Acme"
ROW = {"id": uuid.UUID(TENANT), "name": NAME, "status": "active", "plan_tier": "free", "created_at": datetime.now(timezone.utc), "deleted_at": None}


def client(monkeypatch, dev_headers, role, status=TenantStatus.ACTIVE, restored=uuid.UUID(TENANT)):
    conn = FakeConn(access(role, status=status), row=ROW, vals={"UPDATE tenants": restored, "SELECT name": NAME})
    patch_tenant_conn(monkeypatch, conn, service)
    from app.modules.user.tenants.routes import router
    return make_client(router), conn


@pytest.mark.parametrize("role,status", [(TenantRole.OWNER, 200), (TenantRole.ADMIN, 200), (TenantRole.MEMBER, 403)])
def test_rename_is_for_owner_and_admin(monkeypatch, dev_headers, role, status):
    c, conn = client(monkeypatch, dev_headers, role)
    assert c.patch(T, json={"name": "  New  "}).status_code == status
    assert any(args[1:] == (TENANT, "New") or args == (TENANT, "New") for _, args in conn.calls) == (status == 200)


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_rename_rejects_empty_and_oversized_names(monkeypatch, dev_headers, name):
    c, _ = client(monkeypatch, dev_headers, TenantRole.OWNER)
    assert c.patch(T, json={"name": name}).status_code == 422


def test_every_member_reads_the_tenant_and_the_path_must_match(monkeypatch, dev_headers):
    c, _ = client(monkeypatch, dev_headers, TenantRole.MEMBER)
    assert c.get(T).json()["role"] == "member"
    assert c.get("/v1/tenants/00000000-0000-0000-0000-000000000000").status_code == 404


def test_only_an_owner_deletes_and_must_type_the_name(monkeypatch, dev_headers):
    c, _ = client(monkeypatch, dev_headers, TenantRole.ADMIN)
    assert c.post(f"{T}:delete", json={"confirm_name": NAME}).status_code == 403
    c, conn = client(monkeypatch, dev_headers, TenantRole.OWNER)
    r = c.post(f"{T}:delete", json={"confirm_name": "nope"})
    assert (r.status_code, r.json()["code"]) == (400, ProblemCode.CONFIRMATION_MISMATCH)
    assert not any("UPDATE tenants" in s for s, _ in conn.calls)
    assert c.post(f"{T}:delete", json={"confirm_name": NAME}).status_code == 200
    assert any("UPDATE tenants" in s and TenantStatus.PENDING_DELETE in a for s, a in conn.calls)


def test_a_pending_delete_tenant_answers_403_except_restore(monkeypatch, dev_headers):
    c, _ = client(monkeypatch, dev_headers, TenantRole.OWNER, status=TenantStatus.PENDING_DELETE)
    r = c.get(T)
    assert (r.status_code, r.json()["code"]) == (403, "tenant_pending_delete")
    assert c.patch(T, json={"name": "x"}).status_code == 403
    assert c.post(f"{T}:restore").status_code == 200


def test_restore_only_when_pending_delete(monkeypatch, dev_headers):
    c, _ = client(monkeypatch, dev_headers, TenantRole.OWNER, restored=None)
    r = c.post(f"{T}:restore")
    assert (r.status_code, r.json()["code"]) == (409, ProblemCode.NOT_PENDING_DELETE)
    c, _ = client(monkeypatch, dev_headers, TenantRole.ADMIN, status=TenantStatus.PENDING_DELETE)
    assert c.post(f"{T}:restore").status_code == 403
