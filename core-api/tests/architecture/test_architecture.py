"""The modular-monolith boundaries, as a test.

A layout is only modular for as long as something checks it, so these are
the rules the package structure encodes:

    app/core/         may import app.core only
    app/integrations/ may import app.core and app.integrations
    a bounded context may import app.core, app.integrations, its own
                      package, and another context only through that
                      context's public API (`from app.modules.user.other
                      import thing`) — never `...other.routes`
    app/main.py, app/background.py, app/worker.py  are the composition root: no limits

Contexts live at the leaves of app/modules. The directories above them
(app/modules/user/) are groupings that own no routes, so the depth of the
tree is free to change without touching this file.

Run it directly (`python tests/architecture/test_architecture.py`) for the same checks
without pytest.
"""
import ast
import re
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
MODULES = APP / "modules"
ROOT_FILES = {"main.py", "background.py", "worker.py", "__init__.py"}


def _iter_python_files():
    return sorted(p for p in APP.rglob("*.py") if "__pycache__" not in p.parts)


def _package_dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_dir() and "__pycache__" not in p.parts)


def _subpackages(path: Path) -> list[Path]:
    return [p for p in path.iterdir() if p.is_dir() and p.name != "__pycache__"]


def _context_dirs() -> list[Path]:
    """The bounded contexts: every leaf package under app/modules.

    A directory that only holds other packages is a grouping, so it is the
    leaves — app/modules/user/teams, not app/modules/user — that have to
    publish a router and that the boundary rules are enforced between.
    """
    return [p for p in _package_dirs(MODULES) if not _subpackages(p)]


def _dotted(path: Path) -> str:
    return ".".join(path.relative_to(APP.parent).parts)


def _contexts() -> set[str]:
    return {_dotted(p) for p in _context_dirs()}


def _context_of(module: str, contexts: set[str]) -> str | None:
    """The bounded context a dotted module name belongs to, if any."""
    parts = module.split(".")
    for stop in range(len(parts), 0, -1):
        candidate = ".".join(parts[:stop])
        if candidate in contexts:
            return candidate
    return None


def _imported_modules(path: Path) -> list[tuple[str, int]]:
    """Every `app.`-prefixed module a file imports, with its line number."""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app."):
                    found.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module or not node.module.startswith("app"):
                continue
            for alias in node.names:
                # `from app.core import settings` imports the submodule
                # app.core.settings; `from app.core.db import get_pool` does not.
                candidate = f"{node.module}.{alias.name}"
                exists = (APP.parent / Path(*candidate.split("."))).with_suffix(".py").exists()
                found.append((candidate if exists else node.module, node.lineno))
    return found


def _layer_of(module: str) -> str:
    parts = module.split(".")
    if len(parts) >= 2 and parts[1] in ("core", "integrations", "modules"):
        return parts[1]
    return "root"


def _violations() -> list[str]:
    contexts = _contexts()
    problems = []
    for path in _iter_python_files():
        rel = path.relative_to(APP.parent)
        parts = path.relative_to(APP).parts
        if len(parts) == 1 and path.name in ROOT_FILES:
            continue  # composition root
        layer = parts[0] if parts[0] in ("core", "integrations", "modules") else "root"
        own_context = _context_of(_dotted(path.parent), contexts)

        for target, lineno in _imported_modules(path):
            target_layer = _layer_of(target)
            where = f"{rel}:{lineno} imports {target}"

            if layer == "core" and target_layer != "core":
                problems.append(f"{where} — app/core is the shared kernel and may not depend on {target_layer}")
            elif layer == "integrations" and target_layer == "modules":
                problems.append(f"{where} — app/integrations may not depend on a bounded context")
            elif layer == "modules" and target_layer == "modules":
                other = _context_of(target, contexts)
                # Importing the other context itself is its public API;
                # anything below that name is its internals.
                if other is not None and other != own_context and target != other:
                    problems.append(
                        f"{where} — reaches into {other}'s internals; import it "
                        f"from {other} instead"
                    )
    return problems


def test_layer_boundaries_hold():
    problems = _violations()
    assert not problems, "architecture boundary violations:\n  " + "\n  ".join(problems)


def test_no_two_contexts_import_each_other():
    """Imports between contexts go one way: if A imports B, B (even lazily) must not import A."""
    contexts = _contexts()
    edges = set()
    for path in _iter_python_files():
        own = _context_of(_dotted(path.parent), contexts)
        if own is None:
            continue
        for target, _ in _imported_modules(path):
            other = _context_of(target, contexts)
            if other is not None and other != own:
                edges.add((own, other))
    cycles = sorted(f"{a} <-> {b}" for a, b in edges if a < b and (b, a) in edges)
    assert not cycles, "contexts import each other:\n  " + "\n  ".join(cycles)


def test_every_package_is_a_real_package():
    missing = [
        str(pkg.relative_to(APP.parent))
        for pkg in _package_dirs(MODULES)
        if not (pkg / "__init__.py").exists()
    ]
    assert not missing, (
        "every directory under app/modules needs an __init__.py:\n  " + "\n  ".join(missing)
    )


def test_every_module_exports_a_router():
    missing = []
    for pkg in _context_dirs():
        name = str(pkg.relative_to(MODULES))
        init = pkg / "__init__.py"
        if not init.exists():
            missing.append(f"{name}: no __init__.py")
        elif "router" not in init.read_text(encoding="utf-8"):
            missing.append(f"{name}: __init__ exports no router")
    assert not missing, "modules must publish their router:\n  " + "\n  ".join(missing)


def test_composition_root_registers_every_module():
    main = (APP / "main.py").read_text(encoding="utf-8")
    unwired = sorted(
        str(pkg.relative_to(MODULES))
        for pkg in _context_dirs()
        if f"{pkg.name}.router" not in main
    )
    assert not unwired, f"modules exist but are never mounted in main.py: {unwired}"


# Plan step 4: these modules and clients were deleted; nothing may import or mount them again.
_REMOVED_CODE = re.compile(
    r"modules\.user\.(labeling|preprocessing)\b|\blabelstudio_client\b|\bdatasets\.(tasks|types)\b"
)


def test_removed_legacy_code_is_gone_and_unreferenced():
    assert not (MODULES / "user" / "labeling").exists() and not (MODULES / "user" / "preprocessing").exists()
    assert not (APP / "integrations" / "labelstudio_client.py").exists()
    hits = [str(p.relative_to(APP)) for p in APP.rglob("*.py") if _REMOVED_CODE.search(p.read_text(encoding="utf-8"))]
    assert not hits, f"references to removed legacy modules: {hits}"


# Only app/integrations/data_plane may build a lakeFS, S3 or MLflow client or read their secrets. No exceptions.
_BACKEND_ACCESS = re.compile(
    r"\bboto3\b|settings\.mlflow|\b(lakefs|mlflow)_client\b|^\s*(import|from) mlflow|from app\.core\.secret_files import",
    re.M,
)
_SECRET_NAME = re.compile(r"""['"](lakefs|staging|ingest)['"]|secret_files\.(LAKEFS|STAGING|INGEST)\b""")
# Looping over every secret (REQUIRED) and calling get() reads the data-plane ones too; use secret_files.problems().
_SECRET_ITERATION = re.compile(r"secret_files\.REQUIRED")


def _reaches_backend(source: str, rel: str) -> bool:
    if _BACKEND_ACCESS.search(source):
        return True
    # secret_files.get(<name>) with the name quoted anywhere in the file (core/ defines the names).
    if rel.startswith("app/core/") or "secret_files" not in source:
        return False
    return bool(_SECRET_NAME.search(source)) or bool(_SECRET_ITERATION.search(source))


def test_only_the_data_plane_reaches_lakefs_and_mlflow():
    offenders = [
        rel
        for p in _iter_python_files()
        if "integrations/data_plane" not in p.as_posix()
        and _reaches_backend(p.read_text(encoding="utf-8"), rel := str(p.relative_to(APP.parent)))
    ]
    assert not offenders, "only app/integrations/data_plane may use lakeFS/S3/MLflow clients:\n  " + "\n  ".join(offenders)


def test_the_backend_rule_catches_what_it_claims_to():
    assert _reaches_backend("import boto3", "app/x.py")
    assert _reaches_backend("secret_files.get('staging')", "app/x.py")
    assert _reaches_backend("name = 'lakefs'\nsecret_files.get(name)", "app/x.py")
    assert _reaches_backend("from app.core.secret_files import get", "app/x.py")
    assert not _reaches_backend("secret_files.get('db')", "app/x.py")
    assert _reaches_backend("secret_files.get(secret_files.STAGING)", "app/x.py")
    assert _reaches_backend("for n in secret_files.REQUIRED:\n    secret_files.get(n)", "app/x.py")
    assert not _reaches_backend("secret_files.problems(secret_files.DATA_PLANE)", "app/x.py")


# The data-plane facade (app/integrations/data_plane/__init__.py) is the only way in: outside the package nobody
# imports a submodule, so the gateway's connection rule sits behind one interface.
_DATA_PLANE = "app.integrations.data_plane"
_DATA_PLANE_DIR = APP / "integrations" / "data_plane"
_DATA_PLANE_SUBMODULES = {p.stem for p in _DATA_PLANE_DIR.glob("*.py") if p.stem != "__init__"}


def _data_plane_submodule_imports(source: str, rel: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names if a.name.startswith(_DATA_PLANE + ".")]
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(_DATA_PLANE + "."):
            names = [node.module]
        elif isinstance(node, ast.ImportFrom) and node.module == _DATA_PLANE:
            names = [f"{_DATA_PLANE}.{a.name}" for a in node.names if a.name in _DATA_PLANE_SUBMODULES]
        else:
            continue
        found += [f"{rel}:{node.lineno} imports {n}; import it from {_DATA_PLANE}" for n in names]
    return found


def test_only_the_data_plane_facade_is_imported():
    offenders = [
        v
        for p in _iter_python_files()
        if _DATA_PLANE_DIR not in p.parents
        for v in _data_plane_submodule_imports(p.read_text(encoding="utf-8"), str(p.relative_to(APP.parent)))
    ]
    assert not offenders, "\n  ".join(["outside app/integrations/data_plane, import the facade only:", *offenders])


def test_the_data_plane_facade_rule_catches_what_it_claims_to():
    assert _data_plane_submodule_imports("from app.integrations.data_plane.mlflow import ModelStage", "app/x.py")
    assert _data_plane_submodule_imports("from app.integrations.data_plane import layout", "app/x.py")
    assert _data_plane_submodule_imports("from app.integrations.data_plane import presign_get, access", "app/x.py")
    assert _data_plane_submodule_imports("import app.integrations.data_plane.lakefs", "app/x.py")
    assert not _data_plane_submodule_imports("from app.integrations import data_plane", "app/x.py")
    assert not _data_plane_submodule_imports("from app.integrations.data_plane import ModelStage, presign_get", "app/x.py")
    assert not _data_plane_submodule_imports("import app.integrations.data_plane", "app/x.py")


# Every error raised in app/ is a Problem with a named code (app/core/problem.py). A bare HTTPException would get a
# code derived from its status, and a literal code is a magic string; app/core/problem.py itself is the one exception.
_PROBLEM_MODULE = "app/core/problem.py"


def _error_violations(source: str, rel: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
        if name == "HTTPException":
            found.append(f"{rel}:{node.lineno} raises HTTPException; raise a Problem with a ProblemCode")
        elif name == "Problem":
            code = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "code"), None)
            if isinstance(code, (ast.Constant, ast.JoinedStr)):
                found.append(f"{rel}:{node.lineno} gives Problem a literal code; name it in a ProblemCode")
    return found


def test_every_error_is_a_problem_with_a_named_code():
    offenders = [
        v
        for p in _iter_python_files()
        if (rel := str(p.relative_to(APP.parent))) != _PROBLEM_MODULE
        for v in _error_violations(p.read_text(encoding="utf-8"), rel)
    ]
    assert not offenders, "\n  ".join(["errors must be Problems with a ProblemCode:", *offenders])


def test_the_error_rule_catches_what_it_claims_to():
    assert _error_violations("raise HTTPException(status_code=404)", "app/x.py")
    assert _error_violations("raise fastapi.HTTPException(404)", "app/x.py")
    assert _error_violations('raise Problem(409, "conflict", "x")', "app/x.py")
    assert _error_violations('raise Problem(409, code="conflict", detail="x")', "app/x.py")
    assert _error_violations('raise Problem(409, f"{c}", "x")', "app/x.py")
    assert not _error_violations("raise Problem(409, ProblemCode.LAST_OWNER, 'x')", "app/x.py")
    assert not _error_violations("raise Problem(409, INVALID_PATH, 'x')", "app/x.py")  # a named module constant
    assert not _error_violations("isinstance(e, HTTPException)", "app/x.py")


if __name__ == "__main__":
    failures = 0
    for check in (test_layer_boundaries_hold, test_no_two_contexts_import_each_other, test_every_package_is_a_real_package,
                  test_every_module_exports_a_router,
                  test_composition_root_registers_every_module,
                  test_removed_legacy_code_is_gone_and_unreferenced,
                  test_only_the_data_plane_reaches_lakefs_and_mlflow,
                  test_the_backend_rule_catches_what_it_claims_to,
                  test_only_the_data_plane_facade_is_imported,
                  test_the_data_plane_facade_rule_catches_what_it_claims_to,
                  test_every_error_is_a_problem_with_a_named_code,
                  test_the_error_rule_catches_what_it_claims_to):
        try:
            check()
            print(f"PASS {check.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {check.__name__}\n{exc}")
    sys.exit(1 if failures else 0)
