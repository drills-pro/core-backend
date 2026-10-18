"""require(): the 404/403 table, driven through a real FastAPI app with the database faked."""
import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import Depends
from starlette.exceptions import HTTPException

from app.core import problem
from app.core.authz import Action, AuthzContext, require
from app.core.settings import settings
from tests.support import PROJECT, TENANT, FakeConn, access, make_client, patch_tenant_conn


@pytest.fixture
def serve(monkeypatch, dev_headers):
    http = make_client()
    app = http.app

    @app.get("/p/{project_id}/read")
    async def read(project_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.DATASET_READ))):
        return {"actions": len(ctx.actions)}

    @app.post("/p/{project_id}/deploy")
    async def deploy(project_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.DEPLOYMENT_MANAGE))):
        return {}

    @app.get("/tenant-admin")
    async def tenant_admin(ctx: AuthzContext = Depends(require(Action.TENANT_ADMIN))):
        return {}

    @app.get("/missing")
    async def missing():
        raise problem.not_found()

    def serve(row, project=PROJECT, audits=None):
        patch_tenant_conn(monkeypatch, FakeConn(row, val=project, executed=audits))
        return http

    return serve


def get(serve, path, row, method="get", **kw):
    return getattr(serve(row, **kw), method)(path)


READ_URL, DEPLOY_URL = f"/p/{PROJECT}/read", f"/p/{PROJECT}/deploy"


def test_internal_member_reads(serve):
    assert get(serve, READ_URL, access()).status_code == 200


def test_internal_read_cannot_manage_deployments_403_names_the_action(serve):
    r = get(serve, DEPLOY_URL, access(), "post")
    assert r.status_code == 403
    assert r.headers["content-type"] == "application/problem+json"
    body = r.json()
    assert (body["code"], body["action"], body["status"]) == ("forbidden", "deployment.manage", 403)
    assert "deployment.manage" in body["detail"] and body["request_id"] and body["type"] and body["title"]


def test_default_access_edit_does_not_grant_deploy_manage(serve):
    assert get(serve, DEPLOY_URL, access(default_access="edit"), "post").status_code == 403


def test_bound_deployer_on_internal_project_can_manage(serve):
    assert get(serve, DEPLOY_URL, access(roles=["deployer"]), "post").status_code == 200


def test_private_unbound_is_404(serve):
    assert get(serve, READ_URL, access(visibility="private")).status_code == 404


def test_private_bound_union_of_roles(serve):
    row = access(visibility="private", roles=["dataset_editor", "deployer"])
    assert get(serve, READ_URL, row).status_code == 200
    assert get(serve, DEPLOY_URL, row, "post").status_code == 200
    assert get(serve, DEPLOY_URL, access(visibility="private", roles=["viewer"]), "post").status_code == 403


def test_owner_and_admin_do_everything_on_private_unbound(serve):
    for role in ("owner", "admin"):
        row = access(role=role, visibility="private")
        assert get(serve, DEPLOY_URL, row, "post").status_code == 200


def test_cross_tenant_project_is_404(serve):
    # project_access finds the caller in the tenant but no such project there
    assert get(serve, READ_URL, access(visibility=None, default_access=None, roles=None)).status_code == 404


def test_not_a_tenant_member_is_404(serve, monkeypatch):
    # get_tenant_id already 404s a token caller who is not in the tenant; this is the second line.
    monkeypatch.setattr(settings.auth, "dev_headers", False)
    patch_tenant_conn(monkeypatch, FakeConn(None))
    request = SimpleNamespace(path_params={"project_id": str(PROJECT)})
    with pytest.raises(HTTPException) as e:
        asyncio.run(require(Action.DATASET_READ)(request, TENANT, "u1"))
    assert e.value.status_code == 404


def test_suspended_tenant_is_403_tenant_suspended(serve):
    r = get(serve, READ_URL, access(role="owner", status="suspended"))
    assert (r.status_code, r.json()["code"]) == (403, "tenant_suspended")


def test_tenant_level_action_needs_no_project(serve):
    assert get(serve, "/tenant-admin", access(role="admin", visibility=None)).status_code == 200
    assert get(serve, "/tenant-admin", access(visibility=None)).status_code == 403


def test_authz_404_is_identical_to_missing_resource_404(serve):
    hidden = get(serve, READ_URL, access(visibility="private")).json()
    missing = get(serve, "/missing", access()).json()
    assert hidden.pop("request_id") and missing.pop("request_id")
    assert hidden == missing


def test_request_id_is_echoed_and_unsafe_ones_replaced(serve):
    c = serve(access(visibility="private"))
    r = c.get(READ_URL, headers={"X-Request-Id": "abc-123"})
    assert (r.headers["X-Request-Id"], r.json()["request_id"]) == ("abc-123", "abc-123")
    assert c.get(READ_URL, headers={"X-Request-Id": "bad id\nx"}).headers["X-Request-Id"] != "bad id\nx"


def test_dev_headers_caller_outside_tenant_members_acts_as_owner(serve):
    assert get(serve, DEPLOY_URL, None, "post").status_code == 200


def test_dev_headers_missing_project_is_404(serve):
    assert get(serve, DEPLOY_URL, None, "post", project=None).status_code == 404


def test_admin_write_through_the_implicit_grant_is_audited(serve):
    audits = []
    assert get(serve, DEPLOY_URL, access(role="admin"), "post", audits=audits).status_code == 200
    (_tenant, project, actor, action, entity_type, entity_id), = audits
    assert (project, actor, action) == (PROJECT, "u1", "implicit_access.deployment.manage")
    assert (entity_type, entity_id) == ("project", str(PROJECT))


def test_admin_read_and_bound_or_default_grants_are_not_audited(serve):
    audits = []
    assert get(serve, READ_URL, access(role="owner"), audits=audits).status_code == 200
    assert get(serve, DEPLOY_URL, access(role="admin", roles=["deployer"]), "post", audits=audits).status_code == 200
    assert get(serve, DEPLOY_URL, access(role="member", roles=["deployer"]), "post", audits=audits).status_code == 200
    assert audits == []
