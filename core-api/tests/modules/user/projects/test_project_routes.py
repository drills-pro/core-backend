"""Projects routes: the role / 404 table through the real dependency."""
from datetime import datetime

import pytest

from app.core.authz import DefaultAccess, Visibility
from app.modules.user.projects import routes, service
from tests.support import PROJECT, FakeConn, access, make_client, patch_tenant_conn

PROJECT_ROW = {"id": PROJECT, "name": "p", "status": "active", "team_id": None, "task_type": "legacy_untyped", "created_at": datetime.now(),
               "default_test_dataset_id": None, "settings": {}, "visibility": Visibility.INTERNAL, "default_access": DefaultAccess.READ, "publish_project_metadata": False}


@pytest.fixture
def call(monkeypatch, dev_headers):
    client = make_client(routes.router)

    def go(row, method, path, on_project=True, gallery=False):
        conn = FakeConn(row, row=PROJECT_ROW, val=[PROJECT], vals={"gallery_published": gallery}, result="INSERT 1")
        patch_tenant_conn(monkeypatch, conn, service)
        return client.request(method, f"/v1/projects{f'/{PROJECT}' if on_project else ''}{path}")

    return go


def test_viewer_reads_but_cannot_write_403_names_action(call):
    assert call(access(), "GET", "").status_code == 200
    r = call(access(), "PATCH", "?name=x")
    assert (r.status_code, r.json()["action"]) == (403, "project.write")


def test_editor_cannot_change_visibility_or_default_access_but_can_publish(call):
    row = access(default_access="edit")
    for qs in ("visibility=private", "default_access=edit"):
        r = call(row, "PATCH", f"/visibility?{qs}")
        assert (r.status_code, r.json()["action"]) == (403, "project.member.manage")
    assert call(row, "PATCH", "/publish?publish_project_metadata=true").status_code == 200


def test_project_admin_changes_default_access(call):
    assert call(access(roles=["project_admin"]), "PATCH", "/visibility?default_access=edit").status_code == 200


def test_unknown_task_type_is_422(call):
    assert call(access(), "POST", "?name=x&task_type=bogus", on_project=False).status_code == 422


def test_project_admin_changes_visibility_and_deletes(call):
    row = access(roles=["project_admin"])
    assert call(row, "PATCH", "/visibility?visibility=private").status_code == 200
    assert call(row, "DELETE", "").status_code == 202


def test_editor_cannot_delete(call):
    r = call(access(roles=["editor"]), "DELETE", "")
    assert (r.status_code, r.json()["action"]) == (403, "project.delete")


def test_invalid_visibility_and_default_access_rejected(call):
    row = access(roles=["project_admin"])
    assert call(row, "PATCH", "/visibility?visibility=public").status_code == 422
    assert call(row, "PATCH", "/visibility?default_access=owner").status_code == 422


def test_private_unbound_and_cross_tenant_are_404(call):
    assert call(access(visibility="private"), "GET", "").status_code == 404
    assert call(access(visibility=None, default_access=None), "GET", "").status_code == 404


def test_gallery_project_cannot_be_made_private_but_can_change_default_access(call):
    row = access(roles=["project_admin"])
    r = call(row, "PATCH", "/visibility?visibility=private", gallery=True)
    assert (r.status_code, r.json()["code"]) == (409, "gallery_published")
    assert call(row, "PATCH", "/visibility?visibility=internal", gallery=True).status_code == 200
    assert call(row, "PATCH", "/visibility?default_access=edit", gallery=True).status_code == 200

