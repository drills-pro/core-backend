"""docs/feature-coverage.yaml stays true to the code.

The manifest records every designed capability of training, the model registry, deployments, serving, the predict API,
jobs and datasets, with its status, its code and its tests. These checks keep it from rotting: a cited path or test
function must exist, a cited route must be in the app's OpenAPI, every in-scope route of the app must be recorded (so a
new feature has to be added to the manifest), and a feature marked built must name at least one test.
No services: the OpenAPI is built from the app with APP_ENV=test.
"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "docs" / "feature-coverage.yaml"

AREAS = {"training", "registry", "deployment", "serving", "predict", "jobs", "datasets"}
STATUSES = {"built", "partial", "missing", "deferred"}
REQUIRED = {"id", "area", "title", "source", "status", "code", "tests"}
# A route is in scope when its path contains one of these segments.
IN_SCOPE = re.compile(r"training-runs|training-presets|/models|/deployments|/predict|/datasets|/api-keys|/job-packages|/task-types|/plan-limits")
ROUTE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE) /")


def entries() -> list[dict]:
    return yaml.safe_load(MANIFEST.read_text())["entries"]


def app_routes() -> set[str]:
    from app.main import app

    return {f"{method.upper()} {path}" for path, ops in app.openapi()["paths"].items() for method in ops}


def split(cite: str) -> tuple[str, str | None]:
    path, _, fn = cite.partition("::")
    return path, fn or None


def test_every_entry_has_the_required_fields_and_known_values():
    seen = set()
    for e in entries():
        assert REQUIRED <= e.keys(), f"{e.get('id')}: missing {REQUIRED - e.keys()}"
        assert e["id"] not in seen, f"duplicate id {e['id']}"
        seen.add(e["id"])
        assert e["area"] in AREAS, f"{e['id']}: area {e['area']}"
        assert e["status"] in STATUSES, f"{e['id']}: status {e['status']}"
        assert e["id"].split("-")[0] in {"TRN", "REG", "DEP", "SRV", "PRD", "JOB", "DAT"}, e["id"]
        assert e["status"] == "built" or e.get("gap"), f"{e['id']}: {e['status']} needs a gap line"


def test_cited_code_and_test_paths_exist():
    problems = []
    for e in entries():
        if e["status"] not in ("built", "partial"):
            continue
        for cite in e["code"]:
            if not ROUTE.match(cite) and not (ROOT / cite).exists():
                problems.append(f"{e['id']}: code path {cite} does not exist")
        for cite in e["tests"]:
            path, fn = split(cite)
            file = ROOT / path
            if not file.is_file():
                problems.append(f"{e['id']}: test file {path} does not exist")
            elif fn and not re.search(rf"^\s*(async )?def {re.escape(fn)}\(", file.read_text(), re.M):
                problems.append(f"{e['id']}: {fn} is not defined in {path}")
    assert not problems, "\n".join(problems)


def test_cited_routes_exist_in_the_openapi():
    routes = app_routes()
    missing = [f"{e['id']}: {c}" for e in entries() for c in e["code"] if ROUTE.match(c) and c not in routes]
    assert not missing, "\n".join(missing)


def test_every_in_scope_route_of_the_app_is_recorded():
    cited = {c for e in entries() for c in e["code"]}
    unrecorded = sorted(r for r in app_routes() if IN_SCOPE.search(r) and r not in cited)
    assert not unrecorded, "routes with no entry in docs/feature-coverage.yaml:\n" + "\n".join(unrecorded)


def test_a_built_feature_names_a_test():
    untested = [e["id"] for e in entries() if e["status"] == "built" and not e["tests"]]
    assert not untested, f"built entries with no tests: {untested}"
