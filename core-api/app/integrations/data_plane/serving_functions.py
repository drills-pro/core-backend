"""The functions the shared Serve apps define for a task (its encoder and postprocess), read as text from the ray_serving
package and never imported: core-api depends on neither numpy nor PIL. A task_types row and a bundle's manifest are checked
against them."""
import ast

from app.integrations.data_plane.package_zip import SERVING_PACKAGE_DIR

INPUTS_FILE = "inputs.py"
POSTPROCESS_FILE = "postprocess.py"


def source(name: str) -> str:
    return (SERVING_PACKAGE_DIR / name).read_text()


def _functions(name: str) -> set[str]:
    return {n.name for n in ast.parse(source(name)).body if isinstance(n, ast.FunctionDef)}


def unknown_serving_functions(encoder: str | None, postprocess: str | None) -> list[str]:
    """The names, of those given, that the serve files do not define."""
    missing = [] if encoder is None or encoder in _functions(INPUTS_FILE) else [encoder]
    return missing + ([] if postprocess is None or postprocess in _functions(POSTPROCESS_FILE) else [postprocess])
