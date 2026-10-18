"""Deployments routes: role -> action, 403 names the action, hidden/foreign projects 404, predict stays public."""
import uuid

import pytest

from app.core.authz import Action, Role, Visibility
from app.modules.user.deployments import routes
from tests.support import PROJECT, FakeConn, access, make_client, patch_tenant_conn

DEPLOYMENT = uuid.uuid4()
BASE = f"/v1/projects/{PROJECT}/deployments"


@pytest.fixture
def call(monkeypatch, dev_headers):
    client = make_client(routes.router, raise_server_exceptions=False)

    def run(method, path, row, **kw):
        patch_tenant_conn(monkeypatch, FakeConn(row, row=row), routes)
        return getattr(client, method)(path, **kw)

    return run


def test_viewer_reads_list(call):
    assert call("get", BASE, access()).status_code == 200


def test_viewer_cannot_try_403_names_action(call):
    r = call("post", f"{BASE}/{DEPLOYMENT}/test", access(), json={})
    assert (r.status_code, r.json()["action"]) == (403, Action.DEPLOYMENT_TRY)


def test_viewer_cannot_manage_403_names_action(call):
    r = call("post", f"{BASE}?training_run_id={uuid.uuid4()}", access())
    assert (r.status_code, r.json()["action"]) == (403, Action.DEPLOYMENT_MANAGE)


def test_editor_can_try_but_not_manage(call):
    row = access(roles=[Role.EDITOR])
    assert call("post", f"{BASE}/{DEPLOYMENT}/test", row, json={}).status_code != 403
    assert call("post", f"{BASE}?training_run_id={uuid.uuid4()}", row).status_code == 403


def test_deployer_can_manage(call):
    r = call("post", f"{BASE}?training_run_id={uuid.uuid4()}", access(roles=[Role.DEPLOYER]))
    assert r.status_code != 403


def test_private_unbound_and_foreign_project_are_404(call):
    assert call("get", BASE, access(visibility=Visibility.PRIVATE)).status_code == 404
    assert call("get", BASE, access(visibility=None)).status_code == 404  # project not in this tenant


def test_predict_is_public_not_session_authenticated(call):
    # reaches the X-Predict-Key check (422 without it), never the tenant/authz gate
    assert call("post", f"/v1/predict/{DEPLOYMENT}", access(), json={}).status_code == 422


def test_get_is_read_only_and_hides_internals(call, monkeypatch):
    conn = FakeConn(access(), row=access())
    patch_tenant_conn(monkeypatch, conn, routes)
    c = make_client(routes.router, raise_server_exceptions=False)
    assert c.get(f"{BASE}/{DEPLOYMENT}").status_code == 200
    sql = " ".join(q for q, _ in conn.calls).upper()
    assert "UPDATE" not in sql and "PREDICT_KEY_HASH" not in sql