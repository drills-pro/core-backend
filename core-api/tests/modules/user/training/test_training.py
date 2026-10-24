"""Training routes: job.read / job.create / job.cancel through the real router, database faked."""
import uuid

import pytest

from app.core.authz import TenantRole
from app.modules.user.training import routes
from tests.support import PROJECT, FakeConn, access, make_client, patch_tenant_conn

BASE = f"/v1/projects/{PROJECT}/training-runs"
RUN = uuid.uuid4()


@pytest.fixture
def call(monkeypatch, dev_headers):
    client = make_client(routes.router)

    def go(method, path, row, **kw):
        patch_tenant_conn(monkeypatch, FakeConn(row, row=row), routes)
        return client.request(method, path, **kw)

    return go


def test_viewer_lists_runs(call):
    assert call("GET", BASE, access()).status_code == 200


@pytest.mark.parametrize(
    "method,path,kw,action",
    [
        ("POST", f"{BASE}?dataset_ref=x", {"headers": {"Idempotency-Key": "k"}}, "job.create"),
        ("POST", f"{BASE}/{RUN}/cancel", {}, "job.cancel"),
    ],
)
def test_viewer_denied_403_names_action(call, method, path, kw, action):
    r = call(method, path, access(), **kw)
    assert r.status_code == 403 and r.json()["action"] == action


def test_private_unbound_is_404(call):
    assert call("GET", BASE, access(visibility="private")).status_code == 404


def test_cross_tenant_project_is_404(call):
    assert call("GET", f"{BASE}/{RUN}", access(visibility=None, default_access=None, roles=None)).status_code == 404


def test_create_is_a_501_stub_until_the_trainer_slice(call):
    r = call("POST", f"{BASE}?dataset_ref=x", access(role=TenantRole.ADMIN), headers={"Idempotency-Key": "k"})
    assert (r.status_code, r.json()["code"]) == (501, "not_implemented")
