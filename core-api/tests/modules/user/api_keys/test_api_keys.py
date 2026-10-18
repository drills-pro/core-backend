"""API key management routes through the real dependency: role -> status, database faked."""
import uuid
from datetime import datetime, timezone

import pytest

from app.modules.user.api_keys import routes
from tests.support import PROJECT, FakeConn, access, make_client, patch_tenant_conn

BASE = f"/v1/projects/{PROJECT}/api-keys"
KEY_ID = uuid.uuid4()
NOW = datetime.now(timezone.utc)
KEY_LIST = [{"id": KEY_ID, "name": "k", "prefix": "drl_abcdefgh", "scopes": ["predict"], "model_ids": []}]


@pytest.fixture
def call(monkeypatch, dev_headers):
    client = make_client(routes.router)

    def go(method, path, row, **kw):
        conn = FakeConn(row, row={"id": KEY_ID, "created_at": NOW}, val=KEY_ID, rowset=KEY_LIST)
        patch_tenant_conn(monkeypatch, conn, routes)
        go.log = conn.calls
        return client.request(method, BASE + path, **kw)

    return go


CREATE = {"json": {"name": "prod", "scopes": ["predict"]}}
ROUTES = [("POST", "", CREATE), ("GET", "", {}), ("DELETE", f"/{KEY_ID}", {})]
OK = (200, 201, 204)


@pytest.mark.parametrize(("method", "path", "kw"), ROUTES)
def test_viewer_gets_403_naming_the_action_and_deployer_is_allowed(call, method, path, kw):
    for row in (access(), access(default_access="edit"), access(roles=["editor"])):
        r = call(method, path, row, **kw)
        assert (r.status_code, r.json()["action"]) == (403, "api_key.manage")
    for row in (access(roles=["deployer"]), access(roles=["project_admin"]), access(role="admin", visibility="private")):
        assert call(method, path, row, **kw).status_code in OK


@pytest.mark.parametrize(("method", "path", "kw"), ROUTES)
def test_private_unbound_and_cross_tenant_are_404(call, method, path, kw):
    assert call(method, path, access(visibility="private"), **kw).status_code == 404
    assert call(method, path, access(visibility=None, default_access=None, roles=None), **kw).status_code == 404


def test_secret_is_returned_once_and_only_its_hash_is_stored(call):
    r = call("POST", "", access(roles=["deployer"]), json={"name": "prod", "scopes": ["predict"], "model_ids": [str(uuid.uuid4())]})
    body = r.json()
    assert r.status_code == 201 and body["secret"].startswith("drl_") and body["prefix"] == body["secret"][:12]
    stored = repr(call.log)
    assert body["secret"] not in stored and body["secret"][12:] not in stored
    assert any(isinstance(a, str) and len(a) == 64 for _, args in call.log for a in args)  # the sha256
    assert [a for _, args in call.log for a in args if a == "api_key.create"]  # audited


def test_list_never_shows_secret_or_hash(call):
    r = call("GET", "", access(roles=["deployer"]))
    assert r.status_code == 200 and not {"secret", "key_hash"} & set(r.json()[0])


def test_unknown_scope_is_422(call):
    assert call("POST", "", access(roles=["deployer"]), json={"name": "x", "scopes": ["root"]}).status_code == 422
    assert call("POST", "", access(roles=["deployer"]), json={"name": "x", "scopes": []}).status_code == 422
