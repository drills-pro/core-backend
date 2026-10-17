"""The modular-monolith boundaries, as a test.

A layout is only modular for as long as something checks it, so these are
the rules the package structure encodes:

    app/core/         may import app.core only
    app/integrations/ may import app.core and app.integrations
    a bounded context may import app.core, app.integrations, its own
                      package, and another context only through that
                      context's public API (`from app.modules.user.other
                      import thing`) — never `...other.routes`
    app/main.py, app/background.py  are the composition root: no limits

Contexts live at the leaves of app/modules. The directories above them
(app/modules/user/) are groupings that own no routes, so the depth of the
tree is free to change without touching this file.

Run it directly (`python tests/test_architecture.py`) for the same checks
without pytest.
"""
import ast
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
MODULES = APP / "modules"
ROOT_FILES = {"main.py", "background.py", "__init__.py"}


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


if __name__ == "__main__":
    failures = 0
    for check in (test_layer_boundaries_hold, test_every_package_is_a_real_package,
                  test_every_module_exports_a_router,
                  test_composition_root_registers_every_module):
        try:
            check()
            print(f"PASS {check.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {check.__name__}\n{exc}")
    sys.exit(1 if failures else 0)
