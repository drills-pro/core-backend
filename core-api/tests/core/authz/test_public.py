"""Public, health and self-scoped routes declare public() and need no tenant or token."""
from fastapi.testclient import TestClient

from app.main import create_app

BAD = {"Authorization": "Bearer not-a-token"}


def test_health_needs_no_credentials():
    assert TestClient(create_app()).get("/health").json() == {"status": "ok"}


def test_me_and_tenant_create_still_need_a_token():
    client = TestClient(create_app())
    assert client.get("/v1/me", headers=BAD).status_code == 401
    assert client.post("/v1/tenants", json={"name": "x"}, headers=BAD).status_code == 401
