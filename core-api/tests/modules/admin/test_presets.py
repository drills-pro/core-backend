"""The admin's training presets: the service's rules against a connection it is handed (no pool, no HTTP), and the routes' wiring."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import problem
from app.core.enums import AdminAuditAction, AdminAuditEntity, TaskType
from app.core.presets import PresetBody
from app.core.problem import Problem
from app.modules.admin.presets import routes, service
from app.modules.admin.presets.constants import ProblemCode
from tests.core.test_presets import BODY
from tests.support import FakeConn, TenantCtx, audits, calls_without_audit
from tests.support.presets import preset_row
from tests.support.tx import BEGIN, COMMIT, ROLLBACK, TxConn, statements

URL = "/v1/admin/training-presets"
TASK = TaskType.IMAGE_CLASSIFICATION


def run(coro):
    return asyncio.run(coro)


def body(**over) -> PresetBody:
    return PresetBody.model_validate({**BODY, **over})


# ---- the service ----

def test_put_writes_the_row_as_the_actor_and_audits_it():
    conn = FakeConn(row=preset_row(name="tiny", updated_by="ops"))
    row = run(service.put(conn, TASK, "tiny", body(), "ops"))
    sql, args = calls_without_audit(conn)[-1]
    assert row.updated_by == "ops" and sql.startswith("INSERT INTO task_presets")
    assert args[:2] == ("image-classification", "tiny") and args[11] == "ops" and args[12:] == ("constant", 0, "none", 0, None, None, None, None)
    [(actor, action, entity, entity_id, detail)] = audits(conn)
    assert (actor, action, entity, entity_id) == ("ops", AdminAuditAction.TASK_PRESET_PUT, AdminAuditEntity.TASK_PRESET, "image-classification/tiny")
    assert detail["learning_rate"] == BODY["learning_rate"]


def test_the_write_and_its_audit_row_share_one_transaction_and_fail_together():
    conn = TxConn(row=preset_row())
    run(service.put(conn, TASK, "tiny", body(), "ops"))
    assert statements(conn)[-3:] == ["INSERT INTO task_presets", "INSERT INTO admin_audit", COMMIT] and BEGIN in statements(conn)
    failing = TxConn(row=preset_row(), fail_on="INSERT INTO admin_audit")
    with pytest.raises(RuntimeError):
        run(service.put(failing, TASK, "tiny", body(), "ops"))
    assert statements(failing)[-1] == ROLLBACK


def test_a_task_without_a_trainer_has_no_presets():
    conn = FakeConn()
    with pytest.raises(Problem) as err:
        run(service.put(conn, TaskType.TABULAR_REGRESSION, "x", body(), "ops"))
    assert (err.value.status_code, err.value.code) == (422, ProblemCode.TASK_NOT_TRAINABLE) and audits(conn) == []
    with pytest.raises(Problem):
        run(service.list_for(conn, TaskType.TABULAR_REGRESSION))


def test_list_returns_the_tasks_presets():
    rows = run(service.list_for(FakeConn(rowset=[preset_row(name="a"), preset_row(name="b")]), TASK))
    assert [p.name for p in rows] == ["a", "b"]


def test_deleting_a_missing_preset_is_404_and_leaves_no_audit_row():
    missing = FakeConn(val=None)
    with pytest.raises(Problem) as err:
        run(service.delete(missing, TASK, "nope", "ops"))
    assert err.value.status_code == 404 and audits(missing) == []


def test_deleting_a_preset_audits_it():
    deleted = FakeConn(val=True)
    run(service.delete(deleted, TASK, "small", "ops"))
    assert audits(deleted) == [("ops", AdminAuditAction.TASK_PRESET_DELETE, AdminAuditEntity.TASK_PRESET, "image-classification/small", {})]


def test_an_augmentation_the_task_has_no_trainer_for_is_refused():
    with pytest.raises(Problem) as err:
        run(service.admitted(TASK, body(augmentation="flip")))
    assert (err.value.status_code, err.value.code) == (422, ProblemCode.INVALID_RECIPE)


def test_without_a_hub_the_body_is_accepted_as_it_is():
    original = body()
    assert run(service.admitted(TASK, original)) is original


@pytest.fixture
def hub(monkeypatch):
    """A configured Hub that answers with `facts` (or raises HubUnavailable)."""
    monkeypatch.setattr(service.settings.training, "hub_url", "https://hub.test")
    answer = SimpleNamespace(facts=None)

    async def fetch(repo, rev):
        if answer.facts is None:
            raise service.hub.HubUnavailable("down")
        return answer.facts

    monkeypatch.setattr(service.hub, "fetch", fetch)
    return answer


def _facts(license_id):
    return service.hub.ModelFacts(license=license_id, safetensors_files=1, total_bytes=1, custom_code=False)


def test_a_model_the_policy_rejects_is_refused(hub):
    hub.facts = _facts("cc-by-nc-4.0")
    with pytest.raises(Problem) as err:
        run(service.admitted(TASK, body()))
    assert (err.value.status_code, err.value.code) == (422, ProblemCode.MODEL_NOT_ADMITTED)


def test_the_licence_the_hub_declares_is_recorded_with_the_body(hub):
    hub.facts = _facts("mit")
    admitted = run(service.admitted(TASK, body()))
    assert admitted.base_model.license == "mit" and BODY["base_model"].get("license") != "mit"
    conn = FakeConn(row=preset_row())
    run(service.put(conn, TASK, "tiny", admitted, "ops"))
    assert calls_without_audit(conn)[-1][1][-3] == "mit"  # the licence, before display_name and description


def test_a_hub_that_cannot_be_reached_is_a_502(hub):
    with pytest.raises(Problem) as err:
        run(service.admitted(TASK, body()))
    assert (err.value.status_code, err.value.code) == (502, ProblemCode.HUB_UNAVAILABLE)


# ---- the routes: parse, authorize, call, shape ----

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
    res = call("PUT", f"{URL}/image-classification/tiny", FakeConn(row=preset_row(name="tiny", updated_by="ops")), json=BODY)
    assert res.status_code == 200 and (res.json()["name"], res.json()["updated_by"]) == ("tiny", "ops")


def test_list_answers_with_the_tasks_presets(call):
    res = call("GET", f"{URL}/image-classification", FakeConn(rowset=[preset_row(name="a"), preset_row(name="b")]))
    assert [p["name"] for p in res.json()] == ["a", "b"]


def test_delete_is_204_or_404(call):
    assert call("DELETE", f"{URL}/image-classification/small", FakeConn(val=True)).status_code == 204
    assert call("DELETE", f"{URL}/image-classification/nope", FakeConn(val=None)).status_code == 404


def test_the_services_problems_reach_the_caller_with_their_codes(call):
    assert call("PUT", f"{URL}/tabular-regression/x", FakeConn(), json=BODY).json()["code"] == ProblemCode.TASK_NOT_TRAINABLE
    res = call("PUT", f"{URL}/image-classification/x", FakeConn(), json={**BODY, "augmentation": "flip"})
    assert res.status_code == 422 and res.json()["code"] == ProblemCode.INVALID_RECIPE


def test_a_bad_body_is_422(call):
    assert call("PUT", f"{URL}/image-classification/x", FakeConn(), json={**BODY, "learning_rate": 0}).status_code == 422
