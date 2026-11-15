"""Every mutating /v1/admin route leaves a trail (planning/10-security: platform admin is always audited), or is listed
here with where its trail is, or as an open gap. A route that is in neither fails, so a new admin write cannot ship
silently. Checked by the package of the route calling admin_audit; the per-route rows are asserted in tests/modules/admin."""
import inspect
from pathlib import Path

from app.main import ROUTERS

ADMIN_PREFIX = "/v1/admin"
READ_METHODS = {"GET", "HEAD"}
AUDIT_CALL = "admin_audit.record"

# Routes whose trail is somewhere else, or not written yet: "METHOD path" -> why.
ELSEWHERE_OR_OPEN = {
    "POST /v1/admin/tenants/{tenant_id}/projects": "audit_log row written by the user-side projects.create_project",
    "POST /v1/admin/task-bundles/register": "job_package_audit",
    "PUT /v1/admin/task-bundles/{slug}/{sha256}/status": "job_package_audit",
    "OPEN PUT /v1/admin/task-bundles/catalog/{slug}/enabled": "switches a task type; no row yet",
    "OPEN PUT /v1/admin/task-bundles/catalog/{slug}/presets/{name}/enabled": "switches a preset; no row yet",
    "OPEN PUT /v1/admin/task-bundles/{slug}/settings": "auto-promote switch; no row yet",
    "OPEN POST /v1/admin/task-bundles/{slug}/{sha256}/smoke": "starts a smoke run; no row yet",
    "OPEN POST /v1/admin/job-packages/{name}/publish": "runtime packages; no row yet",
    "OPEN POST /v1/admin/job-packages/{name}/register": "runtime packages; no row yet",
    "OPEN PUT /v1/admin/job-packages/{name}/{sha256}/status": "runtime packages; no row yet",
    "OPEN POST /v1/admin/job-packages/{name}/{sha256}/smoke": "runtime packages; no row yet",
}


def _mutating_admin_routes():
    for router in ROUTERS:
        for route in router.routes:
            if route.path.startswith(ADMIN_PREFIX):
                for method in sorted(route.methods - READ_METHODS):
                    yield f"{method} {route.path}", route


def _package_calls_the_trail(route) -> bool:
    package = Path(inspect.getsourcefile(route.endpoint)).parent
    return any(AUDIT_CALL in file.read_text() or "_audit(" in file.read_text() for file in package.glob("*.py"))


def _keys():
    return {key for key, _ in _mutating_admin_routes()}


def test_every_mutating_admin_route_audits_or_says_where():
    unaccounted = [
        key for key, route in _mutating_admin_routes()
        if key not in ELSEWHERE_OR_OPEN and f"OPEN {key}" not in ELSEWHERE_OR_OPEN and not _package_calls_the_trail(route)
    ]
    assert unaccounted == []


def test_the_list_names_only_routes_that_exist():
    keys = _keys()
    listed = {k.removeprefix("OPEN ") for k in ELSEWHERE_OR_OPEN}
    assert listed <= keys
