import asyncio
import hashlib
import uuid

import pytest
from starlette.exceptions import HTTPException

from app.core.authz import Action, AuthzContext, Role, TenantRole
from app.modules.user.projects import members
from tests.support import FakeConn, patch_tenant_conn

CTX = AuthzContext("t", "u", TenantRole.MEMBER, uuid.uuid4(), frozenset(Action))


ROW = {"id": "i1", "email": "a@b.co", "role": "viewer", "expires_at": "x"}


def _conn(row=ROW, val=1, last_admin=False):
    # fetchval answers the "is this the last project_admin" query from project_members, else `val`
    return FakeConn(row=row, val=val, vals={"project_members": last_admin}, result="DELETE 1")


async def _noop(*a, **k):
    pass


def _patch(monkeypatch, conn):
    patch_tenant_conn(monkeypatch, conn, members)
    monkeypatch.setattr(members.audit, "record", _noop)


def test_create_invitation_stores_only_hash_and_returns_plaintext_once(monkeypatch):
    conn = _conn()
    _patch(monkeypatch, conn)
    out = asyncio.run(members.create_invitation(CTX.project_id, "a@b.co", Role.VIEWER, CTX))
    sql, args = conn.calls[0]
    token = out["token"]
    assert "token_hash" in sql
    assert hashlib.sha256(token.encode()).hexdigest() in args
    assert token not in args


def _accept(monkeypatch, row, email="a@b.co"):
    conn = _conn(row=row)
    _patch(monkeypatch, conn)
    return conn, lambda: asyncio.run(members.accept_invitation(CTX.project_id, "tok", CTX, email))


def test_accept_claims_token_atomically_and_binds_the_invited_role(monkeypatch):
    conn, accept = _accept(monkeypatch, {"id": "i1", "role": "editor"})
    assert accept()["role"] == "editor"
    sql, args = conn.calls[0]
    assert "accepted_at IS NULL" in sql and "lower(i.email) = lower($3)" in sql and "email = NULL" not in sql
    assert hashlib.sha256(b"tok").hexdigest() in args and "a@b.co" in args
    assert any("INSERT INTO project_members" in s and Role.EDITOR in a for s, a in conn.calls)


@pytest.mark.parametrize("row,email", [(None, "a@b.co"), ({"id": "i1", "role": "editor"}, None)])
def test_accept_refusals_are_one_404_and_bind_nothing(monkeypatch, row, email):
    # None row = wrong email, expired, already accepted or unknown token (the UPDATE matched nothing);
    # email None = unverified caller.
    conn, accept = _accept(monkeypatch, row, email)
    with pytest.raises(HTTPException) as e:
        accept()
    assert e.value.status_code == 404
    assert not any("INSERT INTO project_members" in s for s, _ in conn.calls)


def test_verified_email_dev_header_and_unverified_token(monkeypatch):
    monkeypatch.setattr(members.settings.auth, "dev_headers", True)
    assert asyncio.run(members._verified_email(None, "x@y.co")) == "x@y.co"
    monkeypatch.setattr(members.settings.auth, "dev_headers", False)
    assert asyncio.run(members._verified_email(None, "x@y.co")) is None

    async def fake_claims(auth):
        return members.Claims({"sub": "u", "email": "x@y.co", "email_verified": False})

    async def same(c):
        return c

    monkeypatch.setattr(members, "get_claims", fake_claims)
    monkeypatch.setattr(members, "get_profile_claims", same)
    assert asyncio.run(members._verified_email("Bearer t", None)) is None


def test_set_member_roles_writes_one_row_per_role_and_dedupes(monkeypatch):
    conn = _conn()
    _patch(monkeypatch, conn)
    body = members.RoleSet(roles=[Role.DEPLOYER, Role.VIEWER, Role.VIEWER])
    out = asyncio.run(members.set_member_roles(CTX.project_id, "sub-2", body, CTX))
    assert out["roles"] == ["deployer", "viewer"]
    assert conn.calls[-1][1][-1] == ["deployer", "viewer"]  # the INSERT ... unnest($4) argument


def test_set_member_roles_rejects_a_non_tenant_member(monkeypatch):
    _patch(monkeypatch, _conn(val=None))
    with pytest.raises(HTTPException) as e:
        asyncio.run(members.set_member_roles(CTX.project_id, "stranger", members.RoleSet(roles=[Role.VIEWER]), CTX))
    assert (e.value.status_code, e.value.code) == (400, "not_a_tenant_member")


ADMIN_ONLY = members.RoleSet(roles=[Role.VIEWER])
ADMIN_CTX = AuthzContext("t", "u", TenantRole.ADMIN, CTX.project_id, frozenset(Action))
PA_CTX = AuthzContext("t", "u", TenantRole.MEMBER, CTX.project_id, frozenset(Action))


def _downgrade(monkeypatch, ctx, last_admin):
    _patch(monkeypatch, c := _conn(last_admin=last_admin))
    asyncio.run(members.set_member_roles(ctx.project_id, "u", ADMIN_ONLY, ctx))
    return c


def _remove(monkeypatch, ctx, last_admin):
    _patch(monkeypatch, c := _conn(last_admin=last_admin))
    asyncio.run(members.remove_member(ctx.project_id, "u", ctx))
    return c


@pytest.mark.parametrize("act", [_downgrade, _remove])
def test_last_project_admin_cannot_remove_or_downgrade_themselves(monkeypatch, act):
    with pytest.raises(HTTPException) as e:
        act(monkeypatch, PA_CTX, True)
    assert (e.value.status_code, e.value.code) == (409, "last_project_admin")


@pytest.mark.parametrize("act", [_downgrade, _remove])
def test_project_admin_may_leave_when_another_admin_remains(monkeypatch, act):
    act(monkeypatch, PA_CTX, False)  # the query reports "not the last"


@pytest.mark.parametrize("act", [_downgrade, _remove])
def test_tenant_owner_and_admin_may_remove_the_last_project_admin(monkeypatch, act):
    for role in (TenantRole.OWNER, TenantRole.ADMIN):
        c = act(monkeypatch, AuthzContext("t", "u", role, CTX.project_id, frozenset(Action)), True)
        assert not any("NOT EXISTS" in sql for sql, _ in c.calls)


def test_keeping_project_admin_in_the_new_roles_skips_the_guard(monkeypatch):
    _patch(monkeypatch, c := _conn(last_admin=True))
    asyncio.run(members.set_member_roles(PA_CTX.project_id, "u", members.RoleSet(roles=[Role.PROJECT_ADMIN]), PA_CTX))
