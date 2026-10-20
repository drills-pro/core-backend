import asyncio
import uuid

import pytest
from starlette.exceptions import HTTPException

from app.core.auth import Claims
from app.core.authz import Action, TenantRole, allowed_actions
from app.core.authz.policy import TENANT_ACTIONS
from app.modules.user.auth import routes
from app.modules.user.tenant_members import routes as member_routes
from app.modules.user.tenant_members import service as member_service
from tests.support import FakeConn, TenantCtx


def test_capabilities_follow_the_role_map():
    cap = {r: set(routes._capabilities(r)) for r in TenantRole}
    owner_only = {Action.TENANT_DELETE.value, Action.TENANT_TRANSFER_OWNERSHIP.value}
    assert cap[TenantRole.OWNER] == {a.value for a in TENANT_ACTIONS}
    assert cap[TenantRole.ADMIN] == cap[TenantRole.OWNER] - owner_only
    assert cap[TenantRole.MEMBER] == {"project.create", "project.list"}
    assert all(set(routes._capabilities(r)) == {a.value for a in allowed_actions(r, None, None, ()) & TENANT_ACTIONS} for r in TenantRole)


class Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return TenantCtx(self.conn)


def _patch(monkeypatch, conn):
    async def pool():
        return Pool(conn)

    async def allowed(*a):
        return True

    monkeypatch.setattr(routes, "get_pool", pool)
    monkeypatch.setattr(routes.rate_limit, "check", allowed)
    monkeypatch.setattr(routes, "TenantConn", lambda t: TenantCtx(conn))


def test_me_lists_every_tenant_with_capabilities(monkeypatch):
    T1, T2 = uuid.uuid4(), uuid.uuid4()
    rows = [{"id": T1, "name": "A", "role": "owner", "status": "active"}, {"id": T2, "name": "B", "role": "member", "status": "pending_delete"}]
    _patch(monkeypatch, FakeConn(rowset=rows))
    out = asyncio.run(routes.me(Claims({"sub": "u", "email": "a@b.co", "email_verified": True})))
    assert [t.id for t in out.tenants] == [T1, T2]
    assert out.tenants[1].status == "pending_delete"
    assert out.tenants[1].capabilities == ["project.create", "project.list"] and out.email_verified is True


def _create(claims, key="k1"):
    class Req:
        headers, client = {}, None

    return routes.create_tenant(routes.TenantCreate(name="Acme"), Req(), claims, key)


def test_replay_with_the_same_key_returns_the_first_tenant_and_creates_nothing(monkeypatch):
    T = uuid.uuid4()
    conn = FakeConn(row={"id": T, "name": "Acme"})
    _patch(monkeypatch, conn)
    out = asyncio.run(_create(Claims({"sub": "u", "email": "a@b.co", "email_verified": True})))
    assert out.id == T and not conn.executed


def test_first_call_claims_the_key_before_inserting_the_tenant(monkeypatch):
    conn = FakeConn(row=None)
    _patch(monkeypatch, conn)
    asyncio.run(_create(Claims({"sub": "u", "email": "a@b.co", "email_verified": True})))
    sqls = [s for s, _ in conn.calls]
    assert next(i for i, s in enumerate(sqls) if "tenant_create_keys (" in s) < next(i for i, s in enumerate(sqls) if "INSERT INTO tenants" in s)


def test_unverified_email_is_refused(monkeypatch):
    _patch(monkeypatch, FakeConn())
    with pytest.raises(HTTPException) as e:
        asyncio.run(_create(Claims({"sub": "u", "email": "a@b.co", "email_verified": False})))
    assert e.value.status_code == 403


def test_accept_invitation_refusals_are_one_404(monkeypatch):
    for claims, row in [(Claims({"sub": "u", "email": "a@b.co", "email_verified": False}), {"tenant_id": uuid.uuid4(), "role": "member"}),
                        (Claims({"sub": "u", "email": "a@b.co", "email_verified": True}), None)]:
        conn = FakeConn(row=row)
        async def pool(conn=conn):
            return Pool(conn)
        monkeypatch.setattr(member_service, "get_pool", pool)
        with pytest.raises(HTTPException) as e:
            asyncio.run(member_routes.accept_invitation("tok", claims))
        assert e.value.status_code == 404


T9 = uuid.uuid4()


def test_accept_invitation_joins_the_invited_tenant(monkeypatch):
    conn = FakeConn(row={"tenant_id": T9, "role": "admin"})

    async def pool():
        return Pool(conn)

    monkeypatch.setattr(member_service, "get_pool", pool)
    monkeypatch.setattr(member_service, "TenantConn", lambda t: TenantCtx(conn))
    out = asyncio.run(member_routes.accept_invitation("tok", Claims({"sub": "u", "email": "a@b.co", "email_verified": True})))
    assert out.tenant_id == T9 and out.role == "admin"
    assert "accept_tenant_invitation" in conn.calls[0][0]
