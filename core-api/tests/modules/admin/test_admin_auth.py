"""Every tenant, activity and model back-office route refuses a caller who is not a platform admin."""
import uuid

import pytest

from app.core.auth import PLATFORM_ADMIN_ROLE
from tests.modules.admin.conftest import AdminConn

ID = uuid.uuid4()
ROUTES = [
    ("GET", "/v1/admin/tenants", None),
    ("POST", "/v1/admin/tenants", {"name": "Acme"}),
    ("GET", "/v1/admin/tenants/summary", None),
    ("GET", f"/v1/admin/tenants/{ID}", None),
    ("GET", f"/v1/admin/tenants/{ID}/projects", None),
    ("GET", f"/v1/admin/tenants/{ID}/members", None),
    ("GET", f"/v1/admin/tenants/{ID}/datasets", None),
    ("POST", f"/v1/admin/tenants/{ID}/projects", {"name": "p", "task_type": "image-classification"}),
    ("PATCH", f"/v1/admin/tenants/{ID}/settings", {"allow_public_models": True}),
    ("POST", f"/v1/admin/tenants/{ID}/status", {"status": "suspended"}),
    ("GET", "/v1/admin/activity", None),
    ("GET", "/v1/admin/activity/summary", None),
    ("GET", "/v1/admin/models", None),
    ("GET", f"/v1/admin/models/{ID}/versions", None),
    ("GET", "/v1/admin/models/summary", None),
]


@pytest.mark.parametrize("method, path, body", ROUTES)
def test_a_token_without_the_platform_admin_role_is_403(admin, token_claims, method, path, body):
    token_claims("some_other_role")
    conn = AdminConn()
    client = admin(conn, headers={"Authorization": "Bearer t"})
    res = client.request(method, path, json=body)
    assert (res.status_code, res.json()["code"]) == (403, "platform_admin_required")
    assert conn.calls == []  # refused before the database was asked anything


@pytest.mark.parametrize("method, path, body", ROUTES)
def test_no_credentials_is_401(admin, method, path, body):
    res = admin(AdminConn(), headers={}).request(method, path, json=body)
    assert (res.status_code, res.json()["code"]) == (401, "unauthenticated")


def test_a_token_with_the_platform_admin_role_gets_through(admin, token_claims):
    token_claims(PLATFORM_ADMIN_ROLE)
    client = admin(AdminConn(rowset=[]), headers={"Authorization": "Bearer t"})
    assert client.get("/v1/admin/activity").status_code == 200
