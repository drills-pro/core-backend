"""model-registry routes: allowed role, denied role (403 + action), no access (404), other tenant (404)."""
import uuid
from datetime import datetime, timezone

import pytest

from app.core.authz import Action, Role, TenantRole, Visibility
from app.modules.user.model_registry import routes, service
from tests.support import PROJECT, FakeConn, access, make_client, patch_tenant_conn

MODEL = uuid.uuid4()
BASE = f"/v1/projects/{PROJECT}/models"
MODEL_ROW = {"mlflow_model_name": "n", "mlflow_version": "1", "stage": "None", "id": MODEL,
             "publish_metadata": True, "publish_artifact": False, "created_at": datetime.now(timezone.utc)}


ADMIN = access(role=TenantRole.ADMIN)
VIEWER = access()  # internal project, default read
HIDDEN = access(visibility=Visibility.PRIVATE)  # private and unbound


@pytest.fixture
def call(monkeypatch, dev_headers):
    client = make_client(routes.router)

    async def transition(*_):
        return None

    monkeypatch.setattr(service.mlflow, "transition_stage", transition)

    def do(method, path, row, found=True, **kw):
        # found=False: RLS hides every child row, so the lookups come back empty.
        conn = FakeConn(row, row=MODEL_ROW if found else None, val=PROJECT if found else None)
        patch_tenant_conn(monkeypatch, conn, service)
        return client.request(method, path, **kw)

    return do


ROUTES = [
    ("get", BASE, Action.MODEL_READ, {}),
    ("post", f"{BASE}/{MODEL}/promote", Action.MODEL_PROMOTE, {"params": {"stage": "Staging"}}),
    ("patch", f"{BASE}/{MODEL}/publish", Action.PROJECT_WRITE, {"json": {"publish_metadata": True}}),
]


@pytest.mark.parametrize(("method", "path", "action", "kw"), ROUTES)
def test_allowed_and_hidden(call, method, path, action, kw):
    assert call(method, path, ADMIN, **kw).status_code == 200  # admin holds every action
    assert call(method, path, HIDDEN, **kw).status_code == 404  # bound to nothing


def test_an_unknown_stage_is_a_400_listing_the_stages(call):
    res = call("post", f"{BASE}/{MODEL}/promote", ADMIN, params={"stage": "Retired"})
    assert res.status_code == 400 and "Staging" in res.json()["detail"]


@pytest.mark.parametrize(("method", "path", "action", "kw"), ROUTES[1:])
def test_viewer_is_denied_naming_the_action(call, method, path, action, kw):
    res = call(method, path, VIEWER, **kw)
    assert res.status_code == 403 and res.json()["action"] == action.value


@pytest.mark.parametrize(("method", "path", "action", "kw"), ROUTES[1:])
def test_child_of_another_tenant_is_404(call, method, path, action, kw):
    assert call(method, path, ADMIN, found=False, **kw).status_code == 404


@pytest.mark.parametrize(("method", "path", "action", "kw"), ROUTES[1:])
def test_model_of_another_project_is_404(call, method, path, action, kw):
    # Authorized against the project in the URL; the handler's AND project_id = $path finds no row.
    assert call(method, path, ADMIN, found=False, **kw).status_code == 404


def test_model_editor_may_promote_but_not_publish(call):
    editor = access(visibility=Visibility.PRIVATE, roles=[Role.MODEL_EDITOR])
    assert call(*ROUTES[1][:2], editor, **ROUTES[1][3]).status_code == 200
    assert call(*ROUTES[2][:2], editor, **ROUTES[2][3]).status_code == 403
