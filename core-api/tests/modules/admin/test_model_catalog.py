"""/v1/admin/model-catalog through the real router, database faked."""
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import problem
from app.core.enums import AdminAuditAction, AdminAuditEntity, CatalogStatus
from app.integrations import hub
from app.modules.admin.model_catalog import routes, service
from app.modules.admin.model_catalog.constants import ProblemCode
from tests.support import FakeConn, TenantCtx, audits, calls_without_audit

URL = "/v1/admin/model-catalog"
ID = uuid.uuid4()
BODY = {
    "repo_id": "org/m", "revision": "a" * 40, "display_name": "M", "task": "image-classification", "license": "apache-2.0",
    "params": 1000,
}


def row(**over):
    now = datetime.now(UTC)
    return {
        "id": ID, "source": "hf", "repo_id": "org/m", "revision": "a" * 40, "display_name": "M", "task": "image-classification",
        "license": "apache-2.0", "params": 1000, "size_bytes": None, "status": CatalogStatus.ENABLED,
        "created_by": "ops", "created_at": now, "updated_by": "ops", "updated_at": now, **over,
    }


class Conn(FakeConn):
    """FakeConn that can raise from fetchrow (a unique violation) or fetchval."""

    def __init__(self, raises=None, **kw):
        super().__init__(**kw)
        self.raises = raises

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if self.raises:
            raise self.raises
        return self.row


@pytest.fixture
def call(monkeypatch):
    def run(method, path, conn, **kw):
        async def get_pool():
            return SimpleNamespace(acquire=lambda: TenantCtx(conn))

        monkeypatch.setattr(service, "get_pool", get_pool)
        app = FastAPI()
        problem.install(app)
        app.dependency_overrides[routes.get_platform_admin] = lambda: "ops"
        app.include_router(routes.router)
        return TestClient(app).request(method, path, **kw)

    return run


def test_list_filters_by_task_and_status(call):
    conn = FakeConn(rowset=[row()])
    res = call("GET", URL, conn, params={"task": "image-classification", "status": "enabled"})
    assert res.status_code == 200 and res.json()[0]["repo_id"] == "org/m"
    assert calls_without_audit(conn)[-1][1] == ("image-classification", CatalogStatus.ENABLED)


def test_list_rejects_an_unknown_status(call):
    assert call("GET", URL, FakeConn(), params={"status": "maybe"}).status_code == 422


def test_get_returns_the_entry_or_404(call):
    assert call("GET", f"{URL}/{ID}", Conn(row=row())).json()["id"] == str(ID)
    assert call("GET", f"{URL}/{ID}", Conn(row=None)).status_code == 404


def test_create_records_the_actor_and_returns_201(call):
    conn = Conn(row=row())
    res = call("POST", URL, conn, json=BODY)
    sql, args = calls_without_audit(conn)[-1]
    assert res.status_code == 201 and sql.startswith("INSERT INTO model_catalog")
    assert args[:2] == ("hf", "org/m") and args[-1] == "ops" and args[-2] == CatalogStatus.ENABLED
    [(actor, action, entity, entity_id, detail)] = audits(conn)
    assert (actor, action, entity, entity_id) == ("ops", AdminAuditAction.MODEL_CATALOG_CREATE, AdminAuditEntity.MODEL_CATALOG, str(ID))
    assert detail["repo_id"] == "org/m" and detail["revision"] == "a" * 40


def test_create_refuses_a_task_the_platform_has_no_row_for(call):
    conn = Conn(val=False)
    res = call("POST", URL, conn, json=BODY)
    assert res.status_code == 422 and res.json()["code"] == ProblemCode.TASK_NOT_SUPPORTED
    assert not any(sql.startswith("INSERT") for sql, _ in conn.calls)


def test_create_refuses_a_licence_off_the_allowlist(call):
    res = call("POST", URL, Conn(), json={**BODY, "license": "cc-by-nc-4.0"})
    assert res.status_code == 422 and res.json()["code"] == ProblemCode.LICENSE_NOT_ALLOWED


@pytest.mark.parametrize("bad", [{"revision": "main"}, {"params": 0}, {"repo_id": ""}, {"status": "maybe"}, {"extra": 1}])
def test_create_validates_the_body(call, bad):
    assert call("POST", URL, Conn(), json={**BODY, **bad}).status_code == 422


def test_create_twice_is_409(call):
    res = call("POST", URL, Conn(raises=asyncpg.UniqueViolationError()), json=BODY)
    assert res.status_code == 409 and res.json()["code"] == ProblemCode.ALREADY_IN_CATALOG


@pytest.fixture
def hub_on(monkeypatch):
    monkeypatch.setattr(service.settings.training, "hub_url", "https://hub.test")


def test_create_takes_the_licence_and_size_the_hub_declares(call, hub_on, monkeypatch):
    async def fetch(repo_id, revision):
        return hub.ModelFacts(license="mit", safetensors_files=1, total_bytes=42, custom_code=False)

    monkeypatch.setattr(service.hub, "fetch", fetch)
    conn = Conn(row=row())
    assert call("POST", URL, conn, json=BODY).status_code == 201
    args = calls_without_audit(conn)[-1][1]
    assert args[5] == "mit" and args[7] == 42


def test_create_refuses_a_model_the_policy_rejects(call, hub_on, monkeypatch):
    async def fetch(repo_id, revision):
        return hub.ModelFacts(license="mit", safetensors_files=0, total_bytes=1, custom_code=True)

    monkeypatch.setattr(service.hub, "fetch", fetch)
    res = call("POST", URL, Conn(), json=BODY)
    assert res.status_code == 422 and res.json()["code"] == ProblemCode.MODEL_NOT_ADMITTED


def test_create_is_502_when_the_hub_is_down(call, hub_on, monkeypatch):
    async def fetch(repo_id, revision):
        raise hub.HubUnavailable("down")

    monkeypatch.setattr(service.hub, "fetch", fetch)
    assert call("POST", URL, Conn(), json=BODY).status_code == 502


def test_patch_changes_metadata_as_the_actor(call):
    conn = Conn(row=row(display_name="New"))
    res = call("PATCH", f"{URL}/{ID}", conn, json={"display_name": "New"})
    sql, args = calls_without_audit(conn)[-1]
    assert res.status_code == 200 and sql.startswith("UPDATE model_catalog") and args == (ID, "New", None, None, None, None, "ops")
    assert audits(conn) == [("ops", AdminAuditAction.MODEL_CATALOG_UPDATE, AdminAuditEntity.MODEL_CATALOG, str(ID), {"display_name": "New"})]


def test_patch_cannot_change_the_repo_or_status(call):
    assert call("PATCH", f"{URL}/{ID}", Conn(), json={"revision": "b" * 40}).status_code == 422
    assert call("PATCH", f"{URL}/{ID}", Conn(), json={"status": "disabled"}).status_code == 422


def test_patch_missing_is_404_and_a_bad_licence_422(call):
    assert call("PATCH", f"{URL}/{ID}", Conn(row=None), json={"display_name": "x"}).status_code == 404
    assert call("PATCH", f"{URL}/{ID}", Conn(), json={"license": "other"}).status_code == 422


def test_enable_sets_the_status_as_the_actor(call):
    conn = Conn(row=row())
    res = call("POST", f"{URL}/{ID}/enable", conn)
    assert res.status_code == 200 and res.json()["status"] == CatalogStatus.ENABLED and calls_without_audit(conn)[-1][1] == (ID, CatalogStatus.ENABLED, "ops")


def test_disable_sets_the_status_as_the_actor(call):
    conn = Conn(row=row(status=CatalogStatus.DISABLED))
    res = call("POST", f"{URL}/{ID}/disable", conn)
    assert res.status_code == 200 and res.json()["status"] == CatalogStatus.DISABLED and calls_without_audit(conn)[-1][1] == (ID, CatalogStatus.DISABLED, "ops")


def test_enable_missing_is_404_and_audits_nothing(call):
    conn = Conn(row=None)
    assert call("POST", f"{URL}/{ID}/enable", conn).status_code == 404 and audits(conn) == []


def test_delete_an_unused_entry_is_204_and_audited(call):
    conn = FakeConn(val=ID)
    assert call("DELETE", f"{URL}/{ID}", conn).status_code == 204
    assert audits(conn) == [("ops", AdminAuditAction.MODEL_CATALOG_DELETE, AdminAuditEntity.MODEL_CATALOG, str(ID), {})]


def test_delete_a_pinned_entry_is_409_and_a_missing_one_404(call):
    conn = FakeConn(vals={"DELETE FROM": None}, val=True)
    pinned = call("DELETE", f"{URL}/{ID}", conn)
    assert pinned.status_code == 409 and pinned.json()["code"] == ProblemCode.IN_USE and audits(conn) == []
    assert call("DELETE", f"{URL}/{ID}", FakeConn(val=None)).status_code == 404


def test_every_route_needs_the_platform_admin(monkeypatch, dev_headers):
    app = FastAPI()
    problem.install(app)
    app.include_router(routes.router)
    assert TestClient(app).get(URL).status_code == 401
