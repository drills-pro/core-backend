"""A task bundle as scripts/build-bundle.sh builds it: manifest.json and the package sources at the zip root. The manifest is
the real one of task-bundles/tasks/image-classification; the sources are stubs that define its entry points."""
import io
import json
import zipfile
from pathlib import Path

from app.core.bundle_manifest import MANIFEST_FILE
from app.integrations.data_plane.job_package import ZIP_TIMESTAMP

SLUG = "image-classification"
REAL = json.loads((Path(__file__).parents[3] / "task-bundles" / "tasks" / SLUG / MANIFEST_FILE).read_text())
REAL.pop("requirements")  # the real pins are checked in test_locks; these tests pass their own, and none means none
SOURCES = {
    "imgcls/__init__.py": b"",
    "imgcls/train.py": b"def train(spec, work, get):\n    return None\n",
    "imgcls/smoke.py": b"def spec_doc(arg):\n    return {}\n",
}


def manifest(**over) -> dict:
    return json.loads(json.dumps(REAL)) | over  # a deep copy: tests change nested values


def bundle_zip(doc: dict | None = None, sources: dict[str, bytes] | None = None, extra: dict[str, bytes] | None = None) -> bytes:
    files = {MANIFEST_FILE: json.dumps(manifest() if doc is None else doc).encode(), **(SOURCES if sources is None else sources), **(extra or {})}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, data in files.items():
            zf.writestr(zipfile.ZipInfo(path, ZIP_TIMESTAMP), data)
    return buf.getvalue()
