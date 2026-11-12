"""Shared harness for the back-office tests: the real routers behind the real get_platform_admin, Postgres faked at
the connection, Zitadel faked at the HTTP transport (so the real app.integrations.zitadel code runs)."""
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import auth, problem
from app.integrations import zitadel
from app.modules.admin.activity import routes as activity_routes
from app.modules.admin.activity import service as activity_service
from app.modules.admin.models import routes as models_routes
from app.modules.admin.models import service as models_service
from app.modules.admin.tenants import detail, routes as tenant_routes
from app.modules.admin.tenants import service as tenants_service
from tests.support import FakeConn, TenantCtx

ADMIN_HEADERS = {"X-Admin-User": "ops-1"}


class AdminConn(FakeConn):
    """FakeConn whose fetch() answers per SQL substring (`fetches`) and whose rows/vals may be an exception to raise."""

    def __init__(self, fetches=None, **kw):
        super().__init__(**kw)
        self.fetches = fetches or {}

    def transaction(self):
        return TenantCtx(None)

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return next((v for key, v in self.fetches.items() if key in sql), self.rowset)

    async def fetchrow(self, sql, *args):
        out = await super().fetchrow(sql, *args)
        if isinstance(out, Exception):
            raise out
        return out


class ZitadelStub:
    """httpx.MockTransport recorder: `routes` maps a URL path suffix to a status/body, or to an exception to raise."""

    def __init__(self):
        self.routes, self.requests = {}, []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for suffix, answer in self.routes.items():
            if request.url.path.endswith(suffix):
                if isinstance(answer, Exception):
                    raise answer
                if callable(answer):
                    answer = answer(request)
                status, body = answer
                return httpx.Response(status, text=body) if isinstance(body, str) else httpx.Response(status, json=body)
        return httpx.Response(404, json={"message": "no stub"})

    def seen(self, suffix):
        return [r for r in self.requests if r.url.path.endswith(suffix)]


@pytest.fixture
def zitadel_stub(monkeypatch):
    stub = ZitadelStub()
    monkeypatch.setattr(zitadel, "_pat_cache", "test-pat")
    monkeypatch.setattr(zitadel, "client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(stub.handle)))
    return stub


@pytest.fixture
def admin(monkeypatch, dev_headers):
    """admin(conn) -> TestClient authenticated by the dev header, every service's pool and TenantConn on `conn`."""

    def build(conn, headers=ADMIN_HEADERS):
        async def get_pool():
            return SimpleNamespace(acquire=lambda: TenantCtx(conn))

        for mod in (tenants_service, activity_service, models_service):
            monkeypatch.setattr(mod, "get_pool", get_pool)
        monkeypatch.setattr(tenants_service, "TenantConn", lambda tenant_id: TenantCtx(conn))
        app = FastAPI()
        problem.install(app)
        for router in (tenant_routes.router, detail.router, activity_routes.router, models_routes.router):
            app.include_router(router)  # tenants' /summary before its /{tenant_id}, as main.py orders them
        return TestClient(app, headers=headers)

    return build


@pytest.fixture
def token_claims(monkeypatch):
    """Make a Bearer token verify to claims with the given roles."""

    def make(*roles):
        raw = {"sub": "user-9", auth.ROLES_CLAIM: {r: {"o": "org"} for r in roles}}
        monkeypatch.setattr(auth, "verify_token", lambda token: auth.Claims(raw, token))

    return make


def zitadel_orgs(**states):
    return (200, {"result": [{"id": org, "state": state} for org, state in states.items()]})
