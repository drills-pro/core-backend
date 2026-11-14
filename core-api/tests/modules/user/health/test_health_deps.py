"""/health/deps names which dependency is down, without credentials, and never raises."""
import asyncio

import httpx
import pytest

from app.core import secret_files
from app.core.settings import settings
from app.modules.user.health import routes
from tests.support import make_client
from tests.support.fake_pool import RecordingConn, patch_pool


class Down(RecordingConn):
    async def execute(self, sql, *args):
        raise ConnectionError("db down")


def _all_up(monkeypatch):
    patch_pool(monkeypatch, routes.db, RecordingConn())
    monkeypatch.setattr(routes.secret_files, "problems", lambda skip=(): [])

    async def ok(*a):
        return "ok"

    monkeypatch.setattr(routes.data_plane, "lakefs_health", ok)
    monkeypatch.setattr(routes.data_plane, "mlflow_health", ok)
    monkeypatch.setattr(routes, "_check_http", ok)


def test_health_is_up_and_needs_no_credentials():
    assert make_client(routes.router, headers={}).get("/health").json() == {"status": "ok"}


def test_every_dependency_reports_ok_without_credentials(monkeypatch):
    _all_up(monkeypatch)
    r = make_client(routes.router, headers={}).get("/health/deps")
    assert r.status_code == 200
    assert r.json() == dict.fromkeys(("postgres", "secrets", "lakefs_s3", "zitadel", "ray", "mlflow"), "ok")


def test_a_dead_database_is_reported_not_raised(monkeypatch):
    _all_up(monkeypatch)
    patch_pool(monkeypatch, routes.db, Down())
    r = make_client(routes.router, headers={}).get("/health/deps")
    assert (r.status_code, r.json()["postgres"]) == (200, "error: db down")
    assert r.json()["mlflow"] == "ok"  # one failure does not hide the others


def test_a_secret_file_that_does_not_load_is_named(monkeypatch):
    monkeypatch.setattr(routes.secret_files, "problems", lambda skip=(): ["secret 'db': missing ['password']"])
    assert routes._check_secrets() == "error: secret 'db': missing ['password']"


@pytest.mark.parametrize("env,skipped", [("prod", False), ("local", True)])
def test_zitadels_secret_is_only_required_in_prod(monkeypatch, env, skipped):
    seen = []
    monkeypatch.setattr(settings, "env", env)
    monkeypatch.setattr(routes.secret_files, "problems", lambda skip=(): seen.append(skip) or [])
    assert routes._check_secrets() == "ok"
    assert secret_files.DATA_PLANE == seen[0][: len(secret_files.DATA_PLANE)]  # the data plane checks its own
    assert ("zitadel" in seen[0]) is skipped


REAL_CLIENT = httpx.AsyncClient


def _http(monkeypatch, handler):
    monkeypatch.setattr(routes.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))


def test_an_http_dependency_is_ok_on_2xx_and_reports_a_failing_status_or_an_unreachable_host(monkeypatch):
    _http(monkeypatch, lambda request: httpx.Response(200))
    assert asyncio.run(routes._check_http("http://ray/api/version")) == "ok"
    _http(monkeypatch, lambda request: httpx.Response(503))
    assert asyncio.run(routes._check_http("http://ray/api/version")).startswith("error: ")

    def unreachable(request):
        raise httpx.ConnectError("no route")

    _http(monkeypatch, unreachable)
    assert asyncio.run(routes._check_http("http://ray/api/version")) == "error: no route"
