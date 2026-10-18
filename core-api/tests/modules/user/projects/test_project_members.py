"""Members, invitations and audit-log routes through the real router: role -> status, database faked."""
import uuid

import pytest

from app.modules.user.projects import members
from tests.support import HEADERS, PROJECT, FakeConn, access, make_client, patch_tenant_conn

BASE = f"/v1/projects/{PROJECT}"
INVITE = {"id": uuid.uuid4(), "email": "a@b.co", "role": "viewer", "expires_at": None}


@pytest.fixture
def call(monkeypatch, dev_headers):
    client = make_client(members.router, headers={**HEADERS, "X-User-Email": "a@b.co"})

    def go(method, path, row, tenant_member=True, **kw):
        # not the last project_admin: every query but the tenant_members lookup answers False
        conn = FakeConn(row, row=INVITE, val=False, vals={"tenant_members": tenant_member}, result="DELETE 1")
        patch_tenant_conn(monkeypatch, conn, members)
        return client.request(method, BASE + path, **kw)

    return go


ADMIN = access(roles=["project_admin"])
MANAGE = [
    ("PUT", "/members/u2", {"json": {"roles": ["viewer"]}}),
    ("DELETE", "/members/u2", {}),
    ("POST", "/invitations?email=a@b.co&role=viewer", {}),
]


@pytest.mark.parametrize(("method", "path", "kw"), MANAGE)
def test_only_project_admin_manages_members_and_invitations(call, method, path, kw):
    for row in (access(), access(default_access="edit"), access(roles=["deployer"])):
        r = call(method, path, row, **kw)
        assert (r.status_code, r.json()["action"]) == (403, "project.member.manage")
    assert call(method, path, ADMIN, **kw).status_code in (200, 201, 204)
    assert call(method, path, access(role="admin", visibility="private"), **kw).status_code in (200, 201, 204)
    assert call(method, path, access(visibility="private"), **kw).status_code == 404


def test_viewer_lists_members_and_private_unbound_is_404(call):
    assert call("GET", "/members", access()).status_code == 200
    assert call("GET", "/members", access(visibility="private")).status_code == 404


def test_binding_a_non_tenant_member_is_400(call):
    r = call("PUT", "/members/stranger", ADMIN, tenant_member=False, json={"roles": ["viewer"]})
    assert (r.status_code, r.json()["code"]) == (400, "not_a_tenant_member")


def test_accept_needs_only_project_list_so_a_private_unbound_invitee_gets_in(call):
    r = call("POST", "/invitations/tok/accept", access(visibility="private"))
    assert (r.status_code, r.json()["role"]) == (200, "viewer")


def test_audit_log_needs_audit_read(call):
    r = call("GET", "/audit-log", access(roles=["editor"]))
    assert (r.status_code, r.json()["action"]) == (403, "audit.read")
    assert call("GET", "/audit-log", ADMIN).status_code == 200
    assert call("GET", "/audit-log", access(visibility="private")).status_code == 404
