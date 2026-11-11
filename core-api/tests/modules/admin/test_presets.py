"""/v1/admin/training-presets through the real router, database faked."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import problem
from app.modules.admin.presets import routes
from tests.core.test_presets import BODY
from tests.support import FakeConn, TenantCtx
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
    sql, args = conn.calls[-1]
    assert res.status_code == 200 and res.json()["updated_by"] == "ops" and sql.startswith("INSERT INTO task_presets")
    assert args[:2] == ("image-classification", "tiny") and args[11] == "ops" and args[12:] == ("constant", 0, "none", 0, None, None)


def test_list_returns_the_tasks_presets(call):
    res = call("GET", f"{URL}/image-classification", FakeConn(rowset=[preset_row(name="a"), preset_row(name="b")]))
    assert [p["name"] for p in res.json()] == ["a", "b"]


def test_deleting_a_missing_preset_is_404(call):
    assert call("DELETE", f"{URL}/image-classification/nope", FakeConn(val=None)).status_code == 404
    assert call("DELETE", f"{URL}/image-classification/small", FakeConn(val=True)).status_code == 204


def test_a_task_without_a_trainer_has_no_presets(call):
    res = call("PUT", f"{URL}/tabular-regression/x", FakeConn(), json=BODY)
    assert res.status_code == 422 and FakeConn().calls == []


def test_a_bad_body_is_422(call):
    assert call("PUT", f"{URL}/image-classification/x", FakeConn(), json={**BODY, "learning_rate": 0}).status_code == 422


def test_an_augmentation_the_task_has_no_trainer_for_is_422(call):
    res = call("PUT", f"{URL}/image-classification/x", FakeConn(), json={**BODY, "augmentation": "flip"})
    assert res.status_code == 422 and res.json()["code"] == routes.INVALID_RECIPE
