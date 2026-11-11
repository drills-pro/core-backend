"""The public API contract is a reviewed diff: the app's OpenAPI document must equal tests/contract/openapi.snapshot.json.

A failing run lists the added / removed / changed operations. If the change is intended, regenerate and commit the snapshot:

    cd core-api && .venv/bin/python -m tests.contract.test_openapi_snapshot --write

Also pins the predict API invariants of planning/24 (operations exist, legacy route is deprecated by header, error table).
"""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("APP_ENV", "test")  # for the --write entry point; pytest sets it in conftest

TESTS = Path(__file__).parents[1]
SNAPSHOT = Path(__file__).with_name("openapi.snapshot.json")
METHODS = {"get", "put", "post", "delete", "patch"}
REGENERATE = "python -m tests.contract.test_openapi_snapshot --write"


def current() -> dict:
    from app.main import create_app

    return create_app().openapi()


def dump(spec: dict) -> str:
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"


def operations(spec: dict) -> dict:
    return {f"{m.upper()} {p}": op for p, item in spec["paths"].items() for m, op in item.items() if m in METHODS}


def diff(old: dict, new: dict) -> list[str]:
    a, b = operations(old), operations(new)
    lines = [f"+ added   {k}" for k in sorted(b.keys() - a.keys())] + [f"- removed {k}" for k in sorted(a.keys() - b.keys())]
    lines += [f"~ changed {k}" for k in sorted(a.keys() & b.keys()) if a[k] != b[k]]
    if old.get("components") != new.get("components"):
        lines.append("~ changed components (schemas)")
    return lines


def test_openapi_matches_the_committed_snapshot():
    expected = json.loads(SNAPSHOT.read_text())
    spec = current()
    assert dump(spec) == dump(expected), "OpenAPI changed:\n" + "\n".join(diff(expected, spec)) + f"\nIf intended, regenerate and commit: {REGENERATE}"


def test_diff_names_added_removed_and_changed_operations():
    old = {"paths": {"/a": {"get": {"x": 1}}, "/b": {"get": {}}}}
    new = {"paths": {"/a": {"get": {"x": 2}}, "/c": {"post": {}}}}
    assert diff(old, new) == ["+ added   POST /c", "- removed GET /b", "~ changed GET /a"]


# --- predict API invariants (planning/24) ---------------------------------------------------------------------------

PREDICT_OPERATIONS = ["POST /v1/predict", "GET /v1/predict/{prediction_id}", "POST /v1/predict/{prediction_id}/cancel", "GET /v1/predict/{prediction_id}/events"]
LEGACY = "POST /v1/predict/{deployment_id}"

# Statuses of planning/24's error table. They are problem+json written by the key dependency and the handler, not declared
# per route, so the generated document lists only 200 and 422 (FastAPI's own validation response). Each other status is
# explained by the test file that proves it (path relative to tests/); a status the document starts declaring needs no entry.
ERROR_STATUSES = {
    "401": "modules/user/deployments/test_model_predict.py",
    "403": "modules/user/deployments/test_model_predict.py",
    "404": "modules/user/deployments/test_model_predict.py",
    "406": "modules/user/deployments/test_predict_stream_refused.py",
    "409": "modules/user/deployments/test_model_predict.py",
    "413": "modules/user/deployments/test_model_predict.py",
    "422": "modules/user/deployments/test_model_predict.py",
    "429": "modules/user/deployments/test_predict_rate_limit.py",
    "502": "modules/user/deployments/test_model_predict.py",
    "503": "modules/user/deployments/test_model_predict.py",
    "504": "modules/user/deployments/test_model_predict.py",
}


def test_predict_operations_exist():
    ops = operations(json.loads(SNAPSHOT.read_text()))
    missing = [o for o in PREDICT_OPERATIONS if o not in ops]
    assert not missing, missing
    assert "200" in ops["POST /v1/predict"]["responses"]


def test_legacy_route_still_exists_and_is_deprecated_by_header_not_by_openapi_flag():
    # ponytail: routes.py signals deprecation with Deprecation/Sunset headers (tested in test_predict_legacy.py); it does not
    # set deprecated=True, so the document must not claim it. If the code starts marking it, flip this assertion.
    legacy = operations(json.loads(SNAPSHOT.read_text()))[LEGACY]
    assert not legacy.get("deprecated")


def test_every_error_status_of_the_predict_table_is_declared_or_explained():
    ops = operations(json.loads(SNAPSHOT.read_text()))
    declared = set().union(*(ops[o]["responses"].keys() for o in PREDICT_OPERATIONS))
    for status, proof in ERROR_STATUSES.items():
        assert status in declared or (TESTS / proof).exists(), f"{status}: {proof} does not exist"


if __name__ == "__main__":
    if "--write" not in sys.argv:
        sys.exit(f"usage: {REGENERATE}")
    SNAPSHOT.write_text(dump(current()))
    print(f"wrote {SNAPSHOT}")
