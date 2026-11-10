"""GET /health/deps reports every dependency by name, without credentials; a failing probe is reported in the body, not as a 5xx."""
from app.modules.user.health import routes
from tests.support import make_client


def test_every_dependency_is_reported_and_one_failure_does_not_fail_the_call(monkeypatch):
    async def ok(*_):
        return "ok"

    async def down(*_):
        return "error: down"

    monkeypatch.setattr(routes, "_check_postgres", down)
    monkeypatch.setattr(routes, "_check_http", ok)
    monkeypatch.setattr(routes, "_check_secrets", lambda: "ok")
    monkeypatch.setattr(routes.lakefs, "health", ok)
    monkeypatch.setattr(routes.mlflow, "health", ok)
    r = make_client(routes.router, headers={}).get("/health/deps")
    assert r.status_code == 200
    assert r.json() == {"postgres": "error: down", "secrets": "ok", "lakefs_s3": "ok", "zitadel": "ok", "ray": "ok", "mlflow": "ok"}
