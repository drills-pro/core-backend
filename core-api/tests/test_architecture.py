"""The modular-monolith boundaries, as a test.

A layout is only modular for as long as something checks it, so these are
the rules the package structure encodes:

    app/core/         may import app.core only
    app/integrations/ may import app.core and app.integrations
    app/modules/<m>/  may import app.core, app.integrations, its own package,
                      and another module only through that module's public
                      API (`from app.modules.other import thing`) — never
                      `app.modules.other.routes`
    app/main.py, app/background.py  are the composition root: no limits

Run it directly (`python tests/test_architecture.py`) for the same checks
without pytest.
"""
import ast
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
ROOT_FILES = {"main.py", "background.py", "__init__.py"}


def _iter_python_files():
    return sorted(p for p in APP.rglob("*.py") if "__pycache__" not in p.parts)


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
                # `from app.core import config` imports the submodule
                # app.core.config; `from app.core.db import get_pool` does not.
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
    problems = []
    for path in _iter_python_files():
        rel = path.relative_to(APP.parent)
        parts = path.relative_to(APP).parts
        if len(parts) == 1 and path.name in ROOT_FILES:
            continue  # composition root
        layer = parts[0] if parts[0] in ("core", "integrations", "modules") else "root"
        own_module = parts[1] if layer == "modules" and len(parts) > 1 else None

        for target, lineno in _imported_modules(path):
            target_layer = _layer_of(target)
            where = f"{rel}:{lineno} imports {target}"

            if layer == "core" and target_layer != "core":
                problems.append(f"{where} — app/core is the shared kernel and may not depend on {target_layer}")
            elif layer == "integrations" and target_layer == "modules":
                problems.append(f"{where} — app/integrations may not depend on a bounded context")
            elif layer == "modules" and target_layer == "modules":
                target_parts = target.split(".")
                other = target_parts[2] if len(target_parts) > 2 else None
                if other is not None and other != own_module and len(target_parts) > 3:
                    problems.append(
                        f"{where} — reaches into {other}'s internals; import it from "
                        f"app.modules.{other} instead"
                    )
    return problems


def test_layer_boundaries_hold():
    problems = _violations()
    assert not problems, "architecture boundary violations:\n  " + "\n  ".join(problems)


def test_every_module_exports_a_router():
    missing = []
    for pkg in sorted(p for p in (APP / "modules").iterdir() if p.is_dir() and p.name != "__pycache__"):
        init = pkg / "__init__.py"
        if not init.exists():
            missing.append(f"{pkg.name}: no __init__.py")
        elif "router" not in init.read_text(encoding="utf-8"):
            missing.append(f"{pkg.name}: __init__ exports no router")
    assert not missing, "modules must publish their router:\n  " + "\n  ".join(missing)


def test_composition_root_registers_every_module():
    main = (APP / "main.py").read_text(encoding="utf-8")
    declared = {p.name for p in (APP / "modules").iterdir() if p.is_dir() and p.name != "__pycache__"}
    unwired = sorted(name for name in declared if f"{name}.router" not in main)
    assert not unwired, f"modules exist but are never mounted in main.py: {unwired}"


if __name__ == "__main__":
    failures = 0
    for check in (test_layer_boundaries_hold, test_every_module_exports_a_router,
                  test_composition_root_registers_every_module):
        try:
            check()
            print(f"PASS {check.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {check.__name__}\n{exc}")
    sys.exit(1 if failures else 0)
