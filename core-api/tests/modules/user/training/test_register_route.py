"""POST /v1/projects/{pid}/models/{run_id}/register: model.write, then the run must exist and hold a model."""
import uuid
from datetime import UTC, datetime

import pytest

from app.core.authz import Action, TenantRole
from app.modules.user.model_registry import ModelVersion
from app.modules.user.training import models_router, run_service
from app.modules.user.training.constants import TrainingStatus
from tests.support import PROJECT, FakeConn, access, make_client, patch_tenant_conn

RUN = uuid.uuid4()
URL = f"/v1/projects/{PROJECT}/models/{RUN}/register"
NAME = {"name": "pets"}
RUN_ROW = {"id": RUN, "status": TrainingStatus.SUCCEEDED, "mlflow_run_id": "mlflow-1", "class_names": ["a"]}
VERSION = ModelVersion(
    id=uuid.uuid4(), mlflow_model_name="t.p.pets", mlflow_version="1", stage="None", created_at=datetime.now(UTC)
)


@pytest.fixture
def call(monkeypatch, dev_headers):
    client = make_client(models_router)
    registered = []

    async def register_run(ctx, project_id, run_id, name, mlflow_run_id):
        registered.append((project_id, run_id, name, mlflow_run_id))
        return VERSION

    monkeypatch.setattr(run_service, "register_run", register_run)

    def go(acc, run=RUN_ROW):
        patch_tenant_conn(monkeypatch, FakeConn(acc, row=run), run_service)
        return client.post(URL, params=NAME)

    go.registered = registered
    return go


def test_viewer_is_denied_naming_the_action(call):
    r = call(access())
    assert r.status_code == 403 and r.json()["action"] == Action.MODEL_WRITE
    assert not call.registered


def test_an_unknown_run_is_404(call):
    assert call(access(role=TenantRole.ADMIN), run=None).status_code == 404
    assert not call.registered


@pytest.mark.parametrize("over", [{"status": TrainingStatus.RUNNING}, {"mlflow_run_id": None}])
def test_a_run_without_a_model_is_409(call, over):
    assert call(access(role=TenantRole.ADMIN), run={**RUN_ROW, **over}).status_code == 409
    assert not call.registered


def test_a_succeeded_run_registers_its_model(call):
    r = call(access(role=TenantRole.ADMIN))
    assert r.status_code == 201 and r.json()["mlflow_version"] == "1"
    assert call.registered == [(PROJECT, RUN, NAME["name"], "mlflow-1")]
