import asyncio
import uuid

import pytest
from starlette.exceptions import HTTPException

from app.core.authz import Action, AuthzContext, Role, TenantRole
from app.modules.user.projects import service
from app.modules.user.projects.schemas import RoleSet
from tests.support import FakeConn, patch_tenant_conn

CTX = AuthzContext("t", "u", TenantRole.MEMBER, uuid.uuid4(), frozenset(Action))


def _conn(val=1, last_admin=False):
    # fetchval answers the "is this the last project_admin" query from project_members, else `val`
    return FakeConn(val=val, vals={"project_members": last_admin}, result="DELETE 1")


async def _noop(*a, **k):
    pass


def _patch(monkeypatch, conn):
    patch_tenant_conn(monkeypatch, conn, service)
    monkeypatch.setattr(service.audit, "record", _noop)


def test_set_member_roles_writes_one_row_per_role_and_dedupes(monkeypatch):
    conn = _conn()
    _patch(monkeypatch, conn)
    body = RoleSet(roles=[Role.DEPLOYER, Role.VIEWER, Role.VIEWER])
    out = asyncio.run(service.set_member_roles(CTX, CTX.project_id, "sub-2", body.roles))
    assert out.roles == ["deployer", "viewer"]
    assert conn.calls[-1][1][-1] == ["deployer", "viewer"]  # the INSERT ... unnest($4) argument


def test_set_member_roles_rejects_a_non_tenant_member(monkeypatch):
    _patch(monkeypatch, _conn(val=None))
    with pytest.raises(HTTPException) as e:
        asyncio.run(service.set_member_roles(CTX, CTX.project_id, "stranger", [Role.VIEWER]))
    assert (e.value.status_code, e.value.code) == (400, "not_a_tenant_member")


ADMIN_ONLY = [Role.VIEWER]
ADMIN_CTX = AuthzContext("t", "u", TenantRole.ADMIN, CTX.project_id, frozenset(Action))
PA_CTX = AuthzContext("t", "u", TenantRole.MEMBER, CTX.project_id, frozenset(Action))


def _downgrade(monkeypatch, ctx, last_admin):
    _patch(monkeypatch, c := _conn(last_admin=last_admin))
    asyncio.run(service.set_member_roles(ctx, ctx.project_id, "u", ADMIN_ONLY))
    return c


def _remove(monkeypatch, ctx, last_admin):
    _patch(monkeypatch, c := _conn(last_admin=last_admin))
    asyncio.run(service.remove_member(ctx, ctx.project_id, "u"))
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
    asyncio.run(service.set_member_roles(PA_CTX, PA_CTX.project_id, "u", [Role.PROJECT_ADMIN]))
