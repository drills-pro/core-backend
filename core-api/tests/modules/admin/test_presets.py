"""/v1/admin/training-presets through the real router, database faked."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import problem
from app.core.enums import AdminAuditAction, AdminAuditEntity
from app.modules.admin.presets import routes
from tests.core.test_presets import BODY
from tests.support import FakeConn, TenantCtx, audits, calls_without_audit
from tests.support.presets import preset_row

URL = "/v1/admin/training-presets"


@pytest.fixture
def call(monkeypatch):
    def run(method, path, conn, **kw):
        async def get_pool():
            return SimpleNamespace(acquire=lambda: TenantCtx(conn))

        monkeypatch.setattr(routes, "get_pool", get_pool)
        app = FastAPI()
        problem.install(app)
        app.dependency_overrides[routes.get_platform_admin] = lambda: "ops"
        app.include_router(routes.router)
        return TestClient(app).request(method, path, **kw)

    return run


def test_put_writes_the_row_as_the_actor_and_returns_it(call):
    conn = FakeConn(row=preset_row(name="tiny", updated_by="ops"))
    res = call("PUT", f"{URL}/image-classification/tiny", conn, json=BODY)
    sql, args = calls_without_audit(conn)[-1]
    assert res.status_code == 200 and res.json()["updated_by"] == "ops" and sql.startswith("INSERT INTO task_presets")
    assert args[:2] == ("image-classification", "tiny") and args[11] == "ops" and args[12:] == ("constant", 0, "none", 0, None, None)
    [(actor, action, entity, entity_id, detail)] = audits(conn)
    assert (actor, action, entity, entity_id) == ("ops", AdminAuditAction.TASK_PRESET_PUT, AdminAuditEntity.TASK_PRESET, "image-classification/tiny")
    assert detail["learning_rate"] == BODY["learning_rate"]


def test_list_returns_the_tasks_presets(call):
    res = call("GET", f"{URL}/image-classification", FakeConn(rowset=[preset_row(name="a"), preset_row(name="b")]))
    assert [p["name"] for p in res.json()] == ["a", "b"]


def test_deleting_a_missing_preset_is_404(call):
    missing = FakeConn(val=None)
    assert call("DELETE", f"{URL}/image-classification/nope", missing).status_code == 404 and audits(missing) == []
    deleted = FakeConn(val=True)
    assert call("DELETE", f"{URL}/image-classification/small", deleted).status_code == 204
    assert audits(deleted) == [("ops", AdminAuditAction.TASK_PRESET_DELETE, AdminAuditEntity.TASK_PRESET, "image-classification/small", {})]


def test_a_task_without_a_trainer_has_no_presets(call):
    res = call("PUT", f"{URL}/tabular-regression/x", FakeConn(), json=BODY)
    assert res.status_code == 422 and FakeConn().calls == []


def test_a_bad_body_is_422(call):
    assert call("PUT", f"{URL}/image-classification/x", FakeConn(), json={**BODY, "learning_rate": 0}).status_code == 422


def test_an_augmentation_the_task_has_no_trainer_for_is_422(call):
    res = call("PUT", f"{URL}/image-classification/x", FakeConn(), json={**BODY, "augmentation": "flip"})
    assert res.status_code == 422 and res.json()["code"] == routes.INVALID_RECIPE


@pytest.fixture
def hub_on(monkeypatch):
    monkeypatch.setattr(routes.settings.training, "hub_url", "https://hub.test")


def test_put_refuses_a_model_the_policy_rejects_and_writes_nothing(call, hub_on, monkeypatch):
    async def fetch(repo, rev):
        return routes.hub.ModelFacts(license="cc-by-nc-4.0", safetensors_files=1, total_bytes=1, custom_code=False)

    monkeypatch.setattr(routes.hub, "fetch", fetch)
    conn = FakeConn(row=preset_row())
    res = call("PUT", f"{URL}/image-classification/tiny", conn, json=BODY)
    assert res.status_code == 422 and res.json()["code"] == routes.MODEL_NOT_ADMITTED and conn.calls == []


def test_put_records_the_licence_the_hub_declares(call, hub_on, monkeypatch):
    async def fetch(repo, rev):
        return routes.hub.ModelFacts(license="mit", safetensors_files=1, total_bytes=1, custom_code=False)

    monkeypatch.setattr(routes.hub, "fetch", fetch)
    conn = FakeConn(row=preset_row())
    assert call("PUT", f"{URL}/image-classification/tiny", conn, json=BODY).status_code == 200
    assert calls_without_audit(conn)[-1][1][-1] == "mit" and BODY["base_model"].get("license") != "mit"


def test_put_is_502_when_the_hub_cannot_be_reached(call, hub_on, monkeypatch):
    async def fetch(repo, rev):
        raise routes.hub.HubUnavailable("down")

    monkeypatch.setattr(routes.hub, "fetch", fetch)
    assert call("PUT", f"{URL}/image-classification/tiny", FakeConn(), json=BODY).status_code == 502
