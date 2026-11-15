"""/v1/admin/task-types through the real router, database faked."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import problem
from app.core.enums import AdminAuditAction, AdminAuditEntity
from app.core.task_types import ServingParams
from app.modules.admin.task_types import routes
from tests.support import FakeConn, TenantCtx, audits
from tests.support.task_types import task_row

URL = "/v1/admin/task-types"
BODY = {"data_kind": "images", "trainer": "ray_training.classification:train", "servable": True, "encoder": "image_encoder",
        "postprocess": "classify", "enabled": True}


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


def test_put_writes_the_row_as_the_actor(call):
    conn = FakeConn(row=task_row(updated_by="ops"))
    res = call("PUT", f"{URL}/image-classification", conn, json=BODY)
    sql, args = conn.calls[0]
    assert res.status_code == 200 and sql.startswith("INSERT INTO task_types")
    assert args == ("image-classification", "images", BODY["trainer"], True, "image_encoder", "classify", ServingParams().model_dump(mode="json"), True, "ops")
    [(actor, action, entity, entity_id, detail)] = audits(conn)
    assert (actor, action, entity, entity_id) == ("ops", AdminAuditAction.TASK_TYPE_PUT, AdminAuditEntity.TASK_TYPE, "image-classification")
    assert detail["trainer"] == BODY["trainer"]


def test_list_returns_every_task_type(call):
    res = call("GET", URL, FakeConn(rowset=[task_row(), task_row("object-detection")]))
    assert [t["slug"] for t in res.json()] == ["image-classification", "object-detection"]


@pytest.mark.parametrize("bad", [{"data_kind": "audio"}, {"trainer": "no colon"}, {"surprise": 1}])
def test_a_bad_body_is_422(call, bad):
    assert call("PUT", f"{URL}/image-classification", FakeConn(), json={**BODY, **bad}).status_code == 422


@pytest.mark.parametrize("bad", [
    {"servable": True, "encoder": None}, {"encoder": "os.system"}, {"serving": {"max_results": 0}},
    {"serving": {"providers": ["TensorrtExecutionProvider"]}}, {"serving": {"score_threshold": 2}}])
def test_a_bad_serving_body_is_422(call, bad):
    assert call("PUT", f"{URL}/image-classification", FakeConn(), json={**BODY, **bad}).status_code == 422


def test_a_serving_function_the_files_do_not_define_is_422(call):
    res = call("PUT", f"{URL}/image-classification", FakeConn(), json={**BODY, "postprocess": "nope"})
    assert res.status_code == 422 and res.json()["code"] == routes.UNKNOWN_SERVING_FUNCTION
