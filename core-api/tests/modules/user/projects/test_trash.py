"""Trash: deleted projects are listed with their restore deadline, delete reports it, and a team-less tenant still gets a project."""
import uuid
from datetime import datetime, timedelta

from app.core.enums import TaskType
from app.modules.user.projects import routes, service
from app.modules.user.projects.constants import PROJECT_RETENTION_DAYS
from app.modules.user.teams import DEFAULT_TEAM_NAME
from app.modules.user.teams.schemas import Team
from tests.modules.user.projects.test_project_service import CTX, _run
from tests.support import PROJECT, FakeConn, access, make_client, patch_tenant_conn

DELETED_AT = datetime(2026, 1, 1)


def test_trash_lists_deleted_projects_with_the_restore_deadline(monkeypatch, dev_headers):
    row = {"id": PROJECT, "name": "p", "created_at": DELETED_AT, "deleted_at": DELETED_AT}
    conn = FakeConn(access(), rowset=[row], val=[PROJECT])
    patch_tenant_conn(monkeypatch, conn, service)
    r = make_client(routes.router).get("/v1/projects/trash")
    assert r.status_code == 200
    item = r.json()["items"][0]
    assert datetime.fromisoformat(item["restorable_until"]) == DELETED_AT + timedelta(days=PROJECT_RETENTION_DAYS)
    sql, args = next(c for c in conn.calls if "status = $2" in c[0])
    assert args[:2] == ([PROJECT], "pending_delete")


def test_delete_says_until_when_it_can_be_restored(monkeypatch, dev_headers):
    conn = FakeConn(access(roles=["project_admin"]), row={"id": PROJECT, "deleted_at": DELETED_AT}, val=[PROJECT])
    patch_tenant_conn(monkeypatch, conn, service)
    r = make_client(routes.router).delete(f"/v1/projects/{PROJECT}")
    assert r.status_code == 202
    assert datetime.fromisoformat(r.json()["restorable_until"]) == DELETED_AT + timedelta(days=PROJECT_RETENTION_DAYS)


def test_create_project_recreates_the_default_team_when_the_tenant_has_none(monkeypatch):
    team_id, made = uuid.uuid4(), []

    async def create_team(conn, tenant_id, name):
        made.append(name)
        return Team(id=team_id, name=name, created_at=DELETED_AT)

    monkeypatch.setattr(service, "create_team", create_team)
    row = {"id": PROJECT, "name": "x", "status": "active", "team_id": team_id, "task_type": TaskType.TABULAR_REGRESSION, "created_at": DELETED_AT}
    monkeypatch.setattr(service, "insert_project", _insert(row))
    out = _run(monkeypatch, FakeConn(val=None), service.create_project, CTX, "x", TaskType.TABULAR_REGRESSION, None)
    assert made == [DEFAULT_TEAM_NAME] and out.team_id == team_id


def _insert(row):
    from app.modules.user.projects.schemas import CreatedProject

    async def insert(conn, tenant_id, name, team_id, task_type, actor):
        assert team_id == row["team_id"]
        return CreatedProject.model_validate({**row, "visibility": "private", "default_access": "read", "gallery_published": False})

    return insert
