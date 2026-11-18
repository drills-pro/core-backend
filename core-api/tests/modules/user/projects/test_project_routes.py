"""Projects routes: the role / 404 table through the real dependency."""
from datetime import datetime

import pytest

from app.core.authz import DefaultAccess, Visibility
from app.modules.user.projects import routes, service
from app.modules.user.projects.constants import Sharing
from tests.support import PROJECT, FakeConn, access, make_client, patch_tenant_conn

PROJECT_ROW = {"id": PROJECT, "name": "p", "status": "active", "team_id": None, "task_type": "image-classification", "created_at": datetime.now(),
               "default_test_dataset_id": None, "settings": {}, "visibility": Visibility.INTERNAL, "default_access": DefaultAccess.READ, "sharing": Sharing.PRIVATE, "gallery_published": False, "deleted_at": datetime.now()}


@pytest.fixture
def call(monkeypatch, dev_headers):
    client = make_client(routes.router)

    def go(row, method, path, on_project=True, **kw):
        conn = FakeConn(row, row=PROJECT_ROW, val=[PROJECT], result="INSERT 1")
        patch_tenant_conn(monkeypatch, conn, service)
        return client.request(method, f"/v1/projects{f'/{PROJECT}' if on_project else ''}{path}", **kw)

    return go


def test_viewer_reads_but_cannot_write_403_names_action(call):
    assert call(access(), "GET", "").status_code == 200
    r = call(access(), "PATCH", "?name=x")
    assert (r.status_code, r.json()["action"]) == (403, "project.write")


def test_editor_cannot_change_visibility_default_access_or_sharing(call):
    row = access(default_access="edit")
    for qs in ("visibility=private", "default_access=edit"):
        r = call(row, "PATCH", f"/visibility?{qs}")
        assert (r.status_code, r.json()["action"]) == (403, "project.member.manage")
    r = call(row, "PUT", "/sharing", json={"sharing": "gallery"})
    assert (r.status_code, r.json()["action"]) == (403, "project.member.manage")


def test_project_admin_changes_default_access(call):
    assert call(access(roles=["project_admin"]), "PATCH", "/visibility?default_access=edit").status_code == 200


def test_unknown_task_type_is_422(call):
    assert call(access(), "POST", "?name=x&task_type=bogus", on_project=False).status_code == 422


def test_reserved_task_type_is_422_not_supported_yet(call):
    r = call(access(), "POST", "?name=x&task_type=text-generation", on_project=False)
    assert (r.status_code, r.json()["code"]) == (422, "task_type_not_supported")
    assert "not supported yet" in r.json()["detail"]


def test_supported_task_type_creates(call):
    assert call(access(), "POST", "?name=x&task_type=tabular-regression", on_project=False).status_code == 201


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


def test_making_a_gallery_project_private_unpublishes_it_with_no_409(call):
    row = access(roles=["project_admin"])
    assert call(row, "PATCH", "/visibility?visibility=private").json()["sharing"] == "private"
    assert call(row, "PATCH", "/visibility?default_access=edit").status_code == 200


def test_project_admin_sets_the_sharing_level_and_a_bad_level_is_422(call):
    row = access(roles=["project_admin"])
    r = call(row, "PUT", "/sharing", json={"sharing": "gallery_downloads"})
    assert (r.status_code, r.json()["id"]) == (200, str(PROJECT))
    assert call(row, "PUT", "/sharing", json={"sharing": "public"}).status_code == 422


def test_restore_is_project_admin_only(call):
    r = call(access(roles=["editor"]), "POST", ":restore")
    assert (r.status_code, r.json()["action"]) == (403, "project.delete")
    assert call(access(roles=["project_admin"]), "POST", ":restore").status_code == 200


def test_restore_of_a_project_that_is_not_pending_delete_is_409(monkeypatch, dev_headers):
    client = make_client(routes.router)
    patch_tenant_conn(monkeypatch, FakeConn(access(roles=["project_admin"]), row=None, val=[PROJECT]), service)
    r = client.post(f"/v1/projects/{PROJECT}:restore")
    assert (r.status_code, r.json()["code"]) == (409, "not_pending_delete")


def test_list_rejects_a_bad_page_token(call):
    assert call(access(), "GET", "?page_token=garbage", on_project=False).status_code == 400


def test_editor_renames_the_project_and_patches_settings(call):
    row = access(roles=["editor"])
    assert call(row, "PATCH", "?name=renamed").status_code == 200
    assert call(row, "PATCH", "/settings", json={"base_model": "m"}).status_code == 200
    r = call(row, "PATCH", "/settings", json={})
    assert (r.status_code, r.json()["code"]) == (400, "nothing_to_update")
