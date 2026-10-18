"""Every route declares what it needs, and each declared project action behaves as policy says for every principal.

The second test stubs each route's handler (only its require() dependency is kept), so no database or
integration is needed: it checks the declaration against ROLE_ACTIONS, not the handler body.
"""
import re
import uuid

import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.core import problem
from app.core.auth import get_platform_admin
from app.core.authz import DefaultAccess, Role, Scope, TenantRole, Visibility, allowed_actions, public, require_key
from app.core.authz.policy import PROJECT_ACTIONS
from app.main import ROUTERS
from tests.support import HEADERS, FakeConn, access, patch_tenant_conn

# ponytail: walks the routers main.py includes; a route added straight to the app object is not seen.
ROUTES = [r for router in ROUTERS for r in router.routes if isinstance(r, APIRoute)]


def _calls(dependant):
    yield dependant.call
    for sub in dependant.dependencies:
        yield from _calls(sub)


def _declared(route):
    return any(hasattr(c, "authz_action") or hasattr(c, "authz_scope") or c in (public, get_platform_admin) for c in _calls(route.dependant))


def test_every_route_declares_an_action():
    assert ROUTES
    undeclared = [f"{sorted(r.methods)} {r.path} ({r.endpoint.__module__})" for r in ROUTES if not _declared(r)]
    assert not undeclared, "routes without require()/require_key()/public():\n" + "\n".join(undeclared)


def test_require_key_counts_as_a_declaration():
    app = FastAPI()
    app.add_api_route("/k", lambda: {}, dependencies=[Depends(require_key(Scope.PREDICT))])
    app.add_api_route("/bare", lambda: {})
    declared = {r.path: _declared(r) for r in app.routes if isinstance(r, APIRoute)}
    assert declared == {"/k": True, "/bare": False}


# (tenant role, visibility, default_access, bound roles): every way a caller can stand towards a project.
PRINCIPALS = [(TenantRole.MEMBER, Visibility.PRIVATE, DefaultAccess.READ, (role,)) for role in Role] + [
    (TenantRole.MEMBER, Visibility.PRIVATE, DefaultAccess.READ, ()),
    (TenantRole.MEMBER, Visibility.INTERNAL, DefaultAccess.READ, ()),
    (TenantRole.MEMBER, Visibility.INTERNAL, DefaultAccess.EDIT, ()),
    (TenantRole.ADMIN, Visibility.PRIVATE, DefaultAccess.READ, ()),
    (TenantRole.OWNER, Visibility.PRIVATE, DefaultAccess.READ, ()),
]
def _require_dep(route):
    return next((c for c in _calls(route.dependant) if hasattr(c, "authz_action")), None)


PROJECT_ROUTES = [(r, _require_dep(r)) for r in ROUTES if getattr(_require_dep(r), "authz_action", None) in PROJECT_ACTIONS]


@pytest.fixture(scope="module")
def stub_app():
    app = FastAPI()
    problem.install(app)
    for route, dep in PROJECT_ROUTES:
        app.add_api_route(route.path, lambda: {}, methods=list(route.methods), dependencies=[Depends(dep)], name=route.unique_id)
    return app


@pytest.mark.parametrize("principal", PRINCIPALS, ids=lambda p: f"{p[0]}-{p[1]}-{p[2]}-{'+'.join(p[3])}")
@pytest.mark.parametrize("route,dep", PROJECT_ROUTES, ids=lambda v: f"{sorted(v.methods)[0]} {v.path}" if isinstance(v, APIRoute) else "")
def test_declared_action_matches_policy(monkeypatch, dev_headers, stub_app, route, dep, principal):
    action = dep.authz_action
    role, visibility, default_access, bound = principal
    patch_tenant_conn(monkeypatch, FakeConn(access(role, visibility, default_access, bound)))
    allowed = allowed_actions(role, visibility, default_access, bound)
    expected = 200 if action in allowed else 403 if allowed & PROJECT_ACTIONS else 404
    path = re.sub(r"\{[^}]+\}", lambda m: "x" if m[0] != "{project_id}" else str(uuid.uuid4()), route.path)
    client = TestClient(stub_app, headers=HEADERS)
    assert client.request(sorted(route.methods)[0], path).status_code == expected
