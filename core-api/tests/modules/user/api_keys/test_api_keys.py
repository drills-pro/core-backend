"""Workspace API key routes through the real dependency: role -> status, database faked."""
import uuid
from datetime import datetime, timezone

import pytest

from app.modules.user.api_keys import routes, service
from tests.support import FakeConn, access, make_client, patch_tenant_conn

BASE = "/v1/api-keys"
KEY_ID = uuid.uuid4()
PROJECT_ID = uuid.uuid4()
NOW = datetime.now(timezone.utc)
KEY_LIST = [{"id": KEY_ID, "name": "k", "prefix": "drl_abcdefgh", "scopes": ["predict"], "model_ids": [], "project_ids": [PROJECT_ID],
             "created_by": "u1", "created_at": NOW, "expires_at": None, "revoked_at": None}]


@pytest.fixture
def call(monkeypatch, dev_headers):
    client = make_client(routes.router)

    def go(method, path, row, **kw):
        conn = FakeConn(row, row={"id": KEY_ID, "created_at": NOW}, val=KEY_ID, vals={"INSERT INTO api_key_projects": 1}, rowset=KEY_LIST)
        patch_tenant_conn(monkeypatch, conn, service)
        go.log = conn.calls
        return client.request(method, BASE + path, **kw)

    return go


CREATE = {"name": "prod", "scopes": ["predict"], "project_ids": [str(PROJECT_ID)]}
ROUTES = [("POST", "", {"json": CREATE}), ("GET", "", {}), ("DELETE", f"/{KEY_ID}", {})]
OK = (200, 201, 204)


@pytest.mark.parametrize(("method", "path", "kw"), ROUTES)
def test_only_admins_and_owners_manage_workspace_keys(call, method, path, kw):
    for row in (access(), access(roles=["deployer"]), access(roles=["project_admin"])):
        r = call(method, path, row, **kw)
        assert (r.status_code, r.json()["action"]) == (403, "tenant.admin")
    assert call(method, path, access(role="admin"), **kw).status_code in OK
    assert call(method, path, access(role="owner"), **kw).status_code in OK


def test_secret_is_returned_once_and_only_its_hash_is_stored(call):
    r = call("POST", "", access(role="admin"), json=CREATE)
    body = r.json()
    assert r.status_code == 201 and body["secret"].startswith("drl_") and body["prefix"] == body["secret"][:12]
    assert body["project_ids"] == [str(PROJECT_ID)]
    stored = repr(call.log)
    assert body["secret"] not in stored and body["secret"][12:] not in stored
    assert any(isinstance(a, str) and len(a) == 64 for _, args in call.log for a in args)  # the sha256
    assert [a for _, args in call.log for a in args if a == "api_key.create"]  # audited


def test_a_project_of_another_workspace_is_refused_and_nothing_is_kept(call, monkeypatch):
    conn = FakeConn(access(role="admin"), row={"id": KEY_ID, "created_at": NOW}, vals={"INSERT INTO api_key_projects": 0})
    patch_tenant_conn(monkeypatch, conn, service)
    r = make_client(routes.router).post(BASE, json=CREATE)
    assert (r.status_code, r.json()["code"]) == (422, "unknown_project")


def test_a_key_limit_is_stored_returned_and_must_be_positive(call):
    r = call("POST", "", access(role="admin"), json={**CREATE, "rate_limit_per_minute": 30})
    assert r.status_code == 201 and r.json()["rate_limit_per_minute"] == 30 and 30 in [a for _, args in call.log for a in args]
    assert call("POST", "", access(role="admin"), json=CREATE).json()["rate_limit_per_minute"] is None
    assert call("POST", "", access(role="admin"), json={**CREATE, "rate_limit_per_minute": 0}).status_code == 422
    assert "rate_limit_per_minute" in call("GET", "", access(role="admin")).json()[0]


def test_list_shows_the_granted_projects_never_the_secret_or_hash(call):
    r = call("GET", "", access(role="admin"))
    assert r.status_code == 200 and not {"secret", "key_hash"} & set(r.json()[0])
    assert r.json()[0]["project_ids"] == [str(PROJECT_ID)]


def test_unknown_scope_or_no_project_is_422(call):
    assert call("POST", "", access(role="admin"), json={**CREATE, "scopes": ["root"]}).status_code == 422
    assert call("POST", "", access(role="admin"), json={**CREATE, "scopes": []}).status_code == 422
    assert call("POST", "", access(role="admin"), json={**CREATE, "project_ids": []}).status_code == 422
