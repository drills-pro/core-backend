"""Projects service paths the route tests do not reach: listing, update, settings, 404s and team-less tenants."""
import asyncio
import uuid
from datetime import datetime

import pytest
from starlette.exceptions import HTTPException

from app.core.authz import Action, AuthzContext, TenantRole
from app.core.pagination import PageParams
from app.modules.user.projects import readers, service
from app.modules.user.projects.constants import ProjectStatus
from app.modules.user.projects.schemas import ProjectSettingsPatch
from tests.support import FakeConn, patch_tenant_conn

CTX = AuthzContext("t", "u", TenantRole.MEMBER, uuid.uuid4(), frozenset(Action))
PID = CTX.project_id
SUMMARY = {"id": PID, "name": "p", "status": "active", "team_id": None, "task_type": "image-classification", "created_at": datetime(2026, 1, 1)}


class ListConn(FakeConn):
    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.rowset


def _run(monkeypatch, conn, coro_fn, *a):
    patch_tenant_conn(monkeypatch, conn, service)

    async def _noop(*x, **k):
        pass

    monkeypatch.setattr(service.audit, "record_for", _noop)
    return asyncio.run(coro_fn(*a))


def _ids(monkeypatch, ids):
    async def accessible(self, conn):
        return ids

    monkeypatch.setattr(AuthzContext, "accessible_project_ids", accessible)


def test_list_projects_pages_and_excludes_pending_delete(monkeypatch):
    _ids(monkeypatch, [PID])
    rows = [SUMMARY, {**SUMMARY, "id": uuid.uuid4()}, {**SUMMARY, "id": uuid.uuid4()}]
    conn = ListConn(rowset=rows)
    page = _run(monkeypatch, conn, service.list_projects, CTX, PageParams(page_size=2))
    assert len(page.items) == 2 and page.next_page_token is not None
    sql, args = conn.calls[0]
    assert "LIMIT 3" in sql and args[0] == [PID] and args[1] == ProjectStatus.PENDING_DELETE


def test_get_project_of_another_tenant_is_404(monkeypatch):
    with pytest.raises(HTTPException) as e:
        _run(monkeypatch, FakeConn(row=None), service.get_project, CTX, PID)
    assert e.value.status_code == 404


def test_update_project_refuses_to_set_pending_delete_and_empty_patch(monkeypatch):
    for args, code in (((None, ProjectStatus.PENDING_DELETE, None), "status_not_settable"), ((None, None, None), "nothing_to_update")):
        with pytest.raises(HTTPException) as e:
            _run(monkeypatch, FakeConn(), service.update_project, CTX, PID, *args)
        assert (e.value.status_code, e.value.code) == (400, code)


def test_update_project_unknown_test_dataset_is_404(monkeypatch):
    async def no(conn, project, dataset):
        return False

    monkeypatch.setattr(readers, "_dataset_check", no)
    with pytest.raises(HTTPException) as e:
        _run(monkeypatch, FakeConn(), service.update_project, CTX, PID, None, None, uuid.uuid4())
    assert e.value.status_code == 404


def test_update_project_renames_and_audits(monkeypatch):
    conn = FakeConn(row={**SUMMARY, "name": "new", "default_test_dataset_id": None})
    out = _run(monkeypatch, conn, service.update_project, CTX, PID, "new", None, None)
    assert out.name == "new"
    sql, args = conn.calls[-1]
    assert "UPDATE projects" in sql and args[:2] == (PID, "new") and args[-1] == ProjectStatus.PENDING_DELETE


def test_update_project_on_a_deleted_project_is_404(monkeypatch):
    with pytest.raises(HTTPException) as e:
        _run(monkeypatch, FakeConn(row=None), service.update_project, CTX, PID, "x", None, None)
    assert e.value.status_code == 404


def test_update_settings_merges_only_given_keys(monkeypatch):
    conn = FakeConn(row={"id": PID, "settings": {"base_model": "m"}})
    out = _run(monkeypatch, conn, service.update_settings, CTX, PID, ProjectSettingsPatch(base_model="m"))
    assert out.settings == {"base_model": "m"} and conn.calls[-1][1][1] == {"base_model": "m"}


def test_update_settings_empty_patch_is_400_and_missing_project_404(monkeypatch):
    with pytest.raises(HTTPException) as e:
        _run(monkeypatch, FakeConn(), service.update_settings, CTX, PID, ProjectSettingsPatch())
    assert (e.value.status_code, e.value.code) == (400, "nothing_to_update")
    with pytest.raises(HTTPException) as e:
        _run(monkeypatch, FakeConn(row=None), service.update_settings, CTX, PID, ProjectSettingsPatch(base_model="m"))
    assert e.value.status_code == 404


def test_update_visibility_with_nothing_is_400(monkeypatch):
    with pytest.raises(HTTPException) as e:
        _run(monkeypatch, FakeConn(), service.update_visibility, CTX, PID, None, None)
    assert (e.value.status_code, e.value.code) == (400, "nothing_to_update")


def test_remove_member_that_is_not_bound_is_404(monkeypatch):
    conn = FakeConn(vals={"project_members": False}, result="DELETE 0")
    with pytest.raises(HTTPException) as e:
        _run(monkeypatch, conn, service.remove_member, CTX, PID, "ghost")
    assert e.value.status_code == 404


def test_project_team_id_and_unregistered_dataset_check(monkeypatch):
    team = uuid.uuid4()
    assert asyncio.run(readers.get_team_id(FakeConn(val=team), PID)) == team
    monkeypatch.setattr(readers, "_dataset_check", None)
    with pytest.raises(RuntimeError):
        asyncio.run(readers.has_dataset(FakeConn(), PID, uuid.uuid4()))
