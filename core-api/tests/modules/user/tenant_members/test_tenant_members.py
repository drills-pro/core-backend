import hashlib
import uuid
from datetime import datetime, timezone

import pytest

from app.core.authz import TenantRole
from app.modules.user.tenant_members import routes, service
from app.modules.user.tenant_members.constants import ProblemCode
from tests.support import TENANT, FakeConn, access, make_client, patch_tenant_conn

OWNER, ADMIN, MEMBER = TenantRole.OWNER, TenantRole.ADMIN, TenantRole.MEMBER
M = f"/v1/tenants/{TENANT}/members"


def client(monkeypatch, dev_headers, role, current=MEMBER, owners=("u1",), row=None):
    """`role` is the caller's tenant role, `current` the target's role, `owners` the tenant's owners."""
    conn = FakeConn(access(role), vals={"SELECT role FROM tenant_members": current}, rowset=[{"user_sub": s} for s in owners], row=row)
    patch_tenant_conn(monkeypatch, conn, service)
    return make_client(routes.router), conn


@pytest.mark.parametrize("role,status", [(OWNER, 200), (ADMIN, 200), (MEMBER, 403)])
def test_only_owner_and_admin_list_members(monkeypatch, dev_headers, role, status):
    c, conn = client(monkeypatch, dev_headers, role)
    conn.rowset = [{"user_sub": "u1", "role": "owner", "created_at": datetime.now(timezone.utc), "email": "a@b.co", "display_name": None}]
    r = c.get(M)
    assert r.status_code == status
    if status == 200:
        assert r.json()[0]["role"] == "owner"


def test_path_tenant_must_match_the_acting_tenant(monkeypatch, dev_headers):
    c, _ = client(monkeypatch, dev_headers, OWNER)
    assert c.get("/v1/tenants/00000000-0000-0000-0000-000000000000/members").status_code == 404


def test_last_owner_cannot_be_demoted_or_removed(monkeypatch, dev_headers):
    c, _ = client(monkeypatch, dev_headers, OWNER, current=OWNER, owners=("u2",))
    r = c.put(f"{M}/u2", json={"role": "admin"})
    assert (r.status_code, r.json()["code"]) == (409, ProblemCode.LAST_OWNER)
    r = c.delete(f"{M}/u2")
    assert (r.status_code, r.json()["code"]) == (409, ProblemCode.LAST_OWNER)


def test_owner_may_demote_when_another_owner_remains(monkeypatch, dev_headers):
    c, conn = client(monkeypatch, dev_headers, OWNER, current=OWNER, owners=("u1", "u2"))
    assert c.put(f"{M}/u2", json={"role": "admin"}).status_code == 200
    assert any("UPDATE tenant_members" in s for s, _ in conn.calls)


@pytest.mark.parametrize("current,new", [(MEMBER, "owner"), (OWNER, "admin")])
def test_admin_cannot_grant_or_change_owner(monkeypatch, dev_headers, current, new):
    c, conn = client(monkeypatch, dev_headers, ADMIN, current=current, owners=("u1", "u2"))
    assert c.put(f"{M}/u2", json={"role": new}).status_code == 403
    assert not any("UPDATE tenant_members" in s for s, _ in conn.calls)


def test_admin_cannot_remove_an_owner_but_removes_a_member_and_their_bindings(monkeypatch, dev_headers):
    c, conn = client(monkeypatch, dev_headers, ADMIN, current=OWNER, owners=("u1", "u2"))
    assert c.delete(f"{M}/u2").status_code == 403
    c, conn = client(monkeypatch, dev_headers, ADMIN, current=MEMBER)
    assert c.delete(f"{M}/u9").status_code == 204
    sqls = [s for s, _ in conn.calls]
    assert any("DELETE FROM project_members" in s for s in sqls) and any("DELETE FROM tenant_members" in s for s in sqls)


def test_unknown_member_is_404(monkeypatch, dev_headers):
    c, _ = client(monkeypatch, dev_headers, OWNER, current=None)
    assert c.put(f"{M}/ghost", json={"role": "admin"}).status_code == 404


def test_invitation_stores_only_the_hash_and_cannot_grant_owner(monkeypatch, dev_headers):
    row = {"id": uuid.uuid4(), "expires_at": datetime.now(timezone.utc), "email": "a@b.co", "role": "member"}
    c, conn = client(monkeypatch, dev_headers, ADMIN, row=row)
    inv = f"/v1/tenants/{TENANT}/invitations"
    assert c.post(inv, json={"email": "a@b.co", "role": "owner"}).status_code == 422
    token = c.post(inv, json={"email": "a@b.co", "role": "member"}).json()["token"]
    sql, args = next(call for call in conn.calls if "INSERT INTO tenant_invitations" in call[0])
    assert hashlib.sha256(token.encode()).hexdigest() in args and token not in args


@pytest.mark.parametrize("role", [MEMBER])
def test_member_cannot_invite(monkeypatch, dev_headers, role):
    c, _ = client(monkeypatch, dev_headers, role)
    assert c.post(f"/v1/tenants/{TENANT}/invitations", json={"email": "a@b.co", "role": "member"}).status_code == 403


O = f"/v1/tenants/{TENANT}/ownership-offer"
OFFER = {"from_sub": "u1", "to_sub": "u2", "step_down": True, "created_at": datetime.now(timezone.utc), "expires_at": datetime.now(timezone.utc)}


def test_direct_promotion_to_owner_needs_the_offer_flow(monkeypatch, dev_headers):
    c, _ = client(monkeypatch, dev_headers, OWNER, current=MEMBER)
    r = c.put(f"{M}/u2", json={"role": "owner"})
    assert (r.status_code, r.json()["code"]) == (409, ProblemCode.USE_OWNERSHIP_OFFER)


@pytest.mark.parametrize("role,status", [(OWNER, 201), (ADMIN, 403), (MEMBER, 403)])
def test_only_an_owner_offers_ownership(monkeypatch, dev_headers, role, status):
    c, conn = client(monkeypatch, dev_headers, role, row=OFFER)
    assert c.post(O, json={"to_sub": "u2", "step_down": True}).status_code == status
    assert any("INSERT INTO ownership_offers" in s for s, _ in conn.calls) == (status == 201)


def test_cannot_offer_to_a_non_member_or_an_existing_owner(monkeypatch, dev_headers):
    c, _ = client(monkeypatch, dev_headers, OWNER, current=OWNER, row=OFFER)
    assert c.post(O, json={"to_sub": "u2"}).json()["code"] == ProblemCode.ALREADY_OWNER
    c, _ = client(monkeypatch, dev_headers, OWNER, current=None, row=OFFER)
    assert c.post(O, json={"to_sub": "ghost"}).status_code == 404


def test_recipient_accepts_becomes_owner_and_the_offerer_steps_down(monkeypatch, dev_headers):
    c, conn = client(monkeypatch, dev_headers, MEMBER, owners=("u1",), row={"from_sub": "u1", "step_down": True})
    r = c.post(f"{O}:accept")
    assert (r.status_code, r.json()["role"]) == (200, "owner")
    updates = [a for s, a in conn.calls if "UPDATE tenant_members" in s]
    assert [(u[1], u[2]) for u in updates] == [("u1", OWNER), ("u1", ADMIN)]  # actor first, then the offerer


def test_accept_without_step_down_keeps_both_owners(monkeypatch, dev_headers):
    c, conn = client(monkeypatch, dev_headers, MEMBER, owners=("u1",), row={"from_sub": "u1", "step_down": False})
    assert c.post(f"{O}:accept").status_code == 200
    assert len([1 for s, _ in conn.calls if "UPDATE tenant_members" in s]) == 1


def test_accept_refusals(monkeypatch, dev_headers):
    c, _ = client(monkeypatch, dev_headers, MEMBER, row=None)  # no open offer for this caller
    assert c.post(f"{O}:accept").status_code == 404
    c, _ = client(monkeypatch, dev_headers, MEMBER, owners=("someone-else",), row={"from_sub": "u1", "step_down": True})
    r = c.post(f"{O}:accept")
    assert (r.status_code, r.json()["code"]) == (409, ProblemCode.OFFER_STALE)


def test_only_the_parties_and_owners_see_or_cancel_the_offer(monkeypatch, dev_headers):
    other = {**OFFER, "from_sub": "x", "to_sub": "y"}
    c, _ = client(monkeypatch, dev_headers, MEMBER, row=other)
    assert c.get(O).status_code == 404 and c.delete(O).status_code == 404
    c, _ = client(monkeypatch, dev_headers, MEMBER, row=OFFER)  # u1 is the offerer here
    assert c.get(O).json()["to_sub"] == "u2" and c.delete(O).status_code == 204
    c, _ = client(monkeypatch, dev_headers, OWNER, row=other)
    assert c.get(O).status_code == 200


def test_sole_owner_cannot_leave_but_anyone_else_can(monkeypatch, dev_headers):
    c, _ = client(monkeypatch, dev_headers, OWNER, current=OWNER, owners=("u1",))
    r = c.post(f"/v1/tenants/{TENANT}:leave")
    assert (r.status_code, r.json()["code"]) == (409, ProblemCode.LAST_OWNER)
    c, conn = client(monkeypatch, dev_headers, OWNER, current=OWNER, owners=("u1", "u2"))
    assert c.post(f"/v1/tenants/{TENANT}:leave").status_code == 204
    c, conn = client(monkeypatch, dev_headers, MEMBER, current=MEMBER, owners=("u9",))
    assert c.post(f"/v1/tenants/{TENANT}:leave").status_code == 204
    assert any("DELETE FROM tenant_members" in s for s, _ in conn.calls)
