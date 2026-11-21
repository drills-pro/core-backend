"""The admin's task types: the service's rules against a connection it is handed (no pool, no HTTP), and the routes' wiring."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import problem
from app.core.enums import AdminAuditAction, AdminAuditEntity, TaskType
from app.core.problem import Problem
from app.core.task_types import ServingParams, TaskTypeBody
from app.modules.admin.task_types import routes, service
from app.modules.admin.task_types.constants import ProblemCode
from tests.support import FakeConn, TenantCtx, audits
from tests.support.task_types import task_row
from tests.support.tx import COMMIT, ROLLBACK, TxConn, statements

URL = "/v1/admin/task-types"
BODY = {"data_kind": "images", "trainer": "ray_training.classification:train", "servable": True, "encoder": "image_encoder",
        "postprocess": "classify", "enabled": True}
TASK = TaskType.IMAGE_CLASSIFICATION


def run(coro):
    return asyncio.run(coro)


def test_put_writes_the_row_as_the_actor_and_audits_it():
    conn = FakeConn(row=task_row(updated_by="ops"))
    run(service.put(conn, TASK, TaskTypeBody.model_validate(BODY), "ops"))
    sql, args = conn.calls[0]
    assert sql.startswith("INSERT INTO task_types")
    assert args == ("image-classification", "images", BODY["trainer"], True, "image_encoder", "classify", ServingParams().model_dump(mode="json"), True, "ops")
    [(actor, action, entity, entity_id, detail)] = audits(conn)
    assert (actor, action, entity, entity_id) == ("ops", AdminAuditAction.TASK_TYPE_PUT, AdminAuditEntity.TASK_TYPE, "image-classification")
    assert detail["trainer"] == BODY["trainer"]


def test_the_write_and_its_audit_row_share_one_transaction_and_fail_together():
    conn = TxConn(row=task_row())
    run(service.put(conn, TASK, TaskTypeBody.model_validate(BODY), "ops"))
    assert statements(conn)[-2:] == ["INSERT INTO admin_audit", COMMIT]
    failing = TxConn(row=task_row(), fail_on="INSERT INTO admin_audit")
    with pytest.raises(RuntimeError):
        run(service.put(failing, TASK, TaskTypeBody.model_validate(BODY), "ops"))
    assert statements(failing)[-1] == ROLLBACK


def test_a_serving_function_the_files_do_not_define_is_refused_before_anything_is_written():
    conn = FakeConn()
    with pytest.raises(Problem) as err:
        run(service.put(conn, TASK, TaskTypeBody.model_validate({**BODY, "postprocess": "nope"}), "ops"))
    assert (err.value.status_code, err.value.code) == (422, ProblemCode.UNKNOWN_SERVING_FUNCTION) and conn.calls == []


@pytest.fixture
def call(monkeypatch):
    def send(method, path, conn, **kw):
        async def get_pool():
            return SimpleNamespace(acquire=lambda: TenantCtx(conn))

        monkeypatch.setattr(routes, "get_pool", get_pool)
        app = FastAPI()
        problem.install(app)
        app.dependency_overrides[routes.get_platform_admin] = lambda: "ops"
        app.include_router(routes.router)
        return TestClient(app).request(method, path, **kw)

    return send


def test_put_answers_with_the_row(call):
    res = call("PUT", f"{URL}/image-classification", FakeConn(row=task_row(updated_by="ops")), json=BODY)
    assert res.status_code == 200 and res.json()["updated_by"] == "ops"


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


def test_a_serving_function_the_files_do_not_define_is_422_with_its_code(call):
    res = call("PUT", f"{URL}/image-classification", FakeConn(), json={**BODY, "postprocess": "nope"})
    assert res.status_code == 422 and res.json()["code"] == ProblemCode.UNKNOWN_SERVING_FUNCTION
