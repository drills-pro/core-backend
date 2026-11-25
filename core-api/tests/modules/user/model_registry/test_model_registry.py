"""model-registry routes: allowed role, denied role (403 + action), no access (404), other tenant (404)."""
import uuid
from datetime import datetime, timezone

import pytest

from app.core.authz import Action, TenantRole, Visibility
from app.modules.user.model_registry import routes, service
from tests.support import PROJECT, FakeConn, access, make_client, patch_tenant_conn

MODEL = uuid.uuid4()
BASE = f"/v1/projects/{PROJECT}/models"
MODEL_ROW = {"mlflow_model_name": "n", "mlflow_version": "1", "id": MODEL,
             "created_at": datetime.now(timezone.utc),
             "model_id": uuid.uuid4()}


ADMIN = access(role=TenantRole.ADMIN)
VIEWER = access()  # internal project, default read
HIDDEN = access(visibility=Visibility.PRIVATE)  # private and unbound


@pytest.fixture
def call(monkeypatch, dev_headers):
    client = make_client(routes.router)

    def do(method, path, row, found=True, **kw):
        # found=False: RLS hides every child row, so the lookups come back empty.
        conn = FakeConn(row, row=MODEL_ROW if found else None, val=PROJECT if found else None)
        patch_tenant_conn(monkeypatch, conn, service)
        return client.request(method, path, **kw)

    return do


ROUTES = [
    ("get", BASE, Action.MODEL_READ, {}),
]


@pytest.mark.parametrize(("method", "path", "action", "kw"), ROUTES)
def test_allowed_and_hidden(call, method, path, action, kw):
    assert call(method, path, ADMIN, **kw).status_code == 200  # admin holds every action
    assert call(method, path, HIDDEN, **kw).status_code == 404  # bound to nothing


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


class FakeResponse:
    def __init__(self):
        self.closed = False

    async def aiter_bytes(self):
        yield b"on"
        yield b"nx"

    async def aclose(self):
        self.closed = True


class FakeClient(FakeResponse):
    pass


def test_download_streams_the_file_audits_and_closes(call, monkeypatch):
    resp, client, seen = FakeResponse(), FakeClient(), []

    async def open_file(tenant, project, run, name):
        seen.append((project, run, name))
        return client, resp

    monkeypatch.setattr(service.data_plane, "open_model_file", open_file)
    r = call("get", f"{BASE}/{MODEL}/download/model.onnx", ADMIN)
    assert (r.status_code, r.content, r.headers["content-type"]) == (200, b"onnx", "application/octet-stream")
    assert seen[0][2] == "model.onnx" and resp.closed and client.closed


def test_download_refuses_a_file_outside_the_allowlist(call):
    assert call("get", f"{BASE}/{MODEL}/download/secrets.txt", ADMIN).status_code == 422


def test_download_of_an_unknown_version_is_404(call):
    assert call("get", f"{BASE}/{MODEL}/download/model.json", ADMIN, found=False).status_code == 404


def test_promote_is_gone(call):
    assert call("post", f"{BASE}/{MODEL}/promote", ADMIN, params={"stage": "Production"}).status_code == 404
