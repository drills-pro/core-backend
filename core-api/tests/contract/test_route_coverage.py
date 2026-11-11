"""Every route of the app is named by some test, found by a static scan of the test sources (no coverage run).

A route counts as exercised when a test file holds a string or f-string that matches its path template, with an expression
standing for each path parameter (`f"/v1/projects/{PROJECT}/deployments"` matches `/v1/projects/{project_id}/deployments`).
When the literal is the argument of `.get/.post/.put/.patch/.delete(...)` the method must match too; a path kept in a
constant or helper counts for every method. A relative piece (`f"{BASE}/stop"`) matches the tail of a template. When a path
matches both a fixed and a parameterised template (`/summary` vs `/{tenant_id}`), only the fixed one is credited.

A new route fails this test until a test names it or it gets a line in UNTESTED with the reason.
"""
import ast
import re
import sys
from pathlib import Path

from app.main import create_app

TESTS = Path(__file__).parents[1]
METHODS = {"get", "post", "put", "patch", "delete"}
ABSOLUTE = ("/v1/", "/internal/", "/health")
EXPR = "\x00"  # stands for an f-string expression inside a scanned literal

# (METHOD, path template): why no test names it. Keep this short; a route that matters gets a test instead.
UNTESTED: dict[tuple[str, str], str] = {
}


def routes() -> set[tuple[str, str]]:
    return {(m, p) for p, item in create_app().openapi()["paths"].items() for m in map(str.upper, item)}


def _literal(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        lit = "".join(v.value if isinstance(v, ast.Constant) else EXPR for v in node.values)
        return re.sub(f"(?<=[^/]){EXPR}+$", "", lit)  # f"/v1/x/datasets{path}": the unknown tail is dropped
    return None


def scan() -> list[tuple[str, str | None, str]]:
    """(file, method or None, literal) for every path-looking string in the tests (this file excluded)."""
    found = []
    for f in TESTS.rglob("*.py"):
        if f == Path(__file__):
            continue
        tree = ast.parse(f.read_text())
        claimed = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in METHODS and node.args:
                if (lit := _literal(node.args[0])) is not None:
                    found.append((str(f), node.func.attr.upper(), lit))
                    claimed.add(id(node.args[0]))
        for node in ast.walk(tree):
            if id(node) not in claimed and (lit := _literal(node)) is not None and "/" in lit:
                found.append((str(f), None, lit))
    return found


def _rx(norm: str) -> str:
    return "[^/]+".join(re.escape(part) for part in norm.split(EXPR))


def _split(template: str, lit: str) -> str | None:
    """For a relative piece that ends `template`: the part of the template before it (the file must name that base); else None."""
    norm = re.sub(r"\{[^}]*\}", EXPR, template)
    rest = lit.split("?")[0].lstrip(EXPR)  # a leading expression is the base URL constant
    if re.fullmatch(":[a-z]+", rest):  # an action suffix: f"{T}:delete"
        return norm.removesuffix(rest) if norm.endswith(rest) else None
    segs = rest.split("/")[1:]
    parts = norm.split("/")
    if not rest.startswith("/") or not re.search("[a-z]", rest) or len(segs) >= len(parts) or not segs:
        return None
    if all(re.fullmatch(_rx(t), g.replace(EXPR, "x")) for t, g in zip(parts[-len(segs) :], segs)):
        return "/".join(parts[: -len(segs)])
    return None


def covered(all_routes: set[tuple[str, str]]) -> set[tuple[str, str]]:
    templates = {p for _, p in all_routes}
    found = scan()
    bases: dict[str, set[str]] = {}  # file -> the absolute literals it holds
    for f, _, lit in found:
        if lit.startswith(ABSOLUTE):
            bases.setdefault("support" if "/support/" in f else f, set()).add(lit.replace(EXPR, "x"))
    done = set()
    for f, method, lit in found:
        if lit.startswith(ABSOLUTE):
            lit = lit.split("?")[0]
            hits = [p for p in templates if re.fullmatch(_rx(re.sub(r"\{[^}]*\}", EXPR, p)), lit.replace(EXPR, "x"))]
            if hits:  # a fixed template shadows a parameterised one
                fewest = min(p.count("{") for p in hits)
                hits = [p for p in hits if p.count("{") == fewest]
        else:
            hits = []
            for p in templates:
                base = _split(p, lit)
                if base is not None and any(re.fullmatch(_rx(base), b) for b in bases.get(f, set()) | bases.get("support", set())):
                    hits.append(p)
        done |= {(m, p) for m, p in all_routes if p in hits and method in (None, m)}
    return done


def _hit(monkeypatch, routes_, lit, base=None):
    monkeypatch.setattr(sys.modules[__name__], "scan", lambda: [("f", None, b) for b in ([base] if base else [])] + [("f", None, lit)])
    return covered(routes_)


def test_the_matching_reads_the_ways_tests_name_a_path(monkeypatch):
    routes_ = {("GET", "/v1/p/{id}/things"), ("POST", "/v1/p/{id}/things/{t}/stop"), ("GET", "/v1/p/summary"), ("GET", "/v1/p/{id}")}
    assert _hit(monkeypatch, routes_, "/v1/p/\x00/things?a=1") == {("GET", "/v1/p/{id}/things")}  # f-string with a query
    assert _hit(monkeypatch, routes_, "/v1/p/summary") == {("GET", "/v1/p/summary")}  # a fixed template shadows the parameterised one
    assert _hit(monkeypatch, routes_, "/\x00/stop", base="/v1/p/\x00/things") >= {("POST", "/v1/p/{id}/things/{t}/stop")}  # relative piece, base named
    assert _hit(monkeypatch, routes_, "/\x00/stop") == set()  # relative piece without a base in the file


def test_every_route_is_named_by_a_test_or_listed_as_untested():
    all_routes = routes()
    untested = sorted(all_routes - covered(all_routes) - UNTESTED.keys())
    assert not untested, "routes no test names (add a test, or a line with a reason to UNTESTED):\n" + "\n".join(f"{m} {p}" for m, p in untested)


def test_untested_lists_only_routes_that_exist_and_are_still_untested():
    all_routes = routes()
    stale = sorted(k for k in UNTESTED if k not in all_routes or k in covered(all_routes))
    assert not stale, f"drop from UNTESTED: {stale}"
