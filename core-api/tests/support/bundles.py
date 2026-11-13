"""A task bundle as CI builds it: manifest.json plus the package sources, zipped under the root Ray unpacks."""
import io
import json
import zipfile

from app.core.bundle_manifest import MANIFEST_FILE
from app.integrations.data_plane.job_package import ZIP_ROOT, ZIP_TIMESTAMP
from tests.support.presets import IMAGENET, LIMITS, REVISION

SLUG = "image-classification"
SOURCES = {
    "imgcls/__init__.py": b"",
    "imgcls/train.py": b"def train(spec, ds, work):\n    return None\n",
    "imgcls/smoke.py": b"from imgcls.train import train as run\n",
    "imgcls/adapter.py": b"class ClassificationAdapter:\n    pass\n",
}


def manifest(**over) -> dict:
    return {
        "schema": 1, "slug": SLUG, "display_name": "Image classification", "data_kind": "images", "contract": 1,
        "trainer": "imgcls.train:train", "smoke": "imgcls.smoke:run",
        "serving": {"adapter": "imgcls.adapter:ClassificationAdapter", "adapter_version": 3}, "servable": True, "monitor": "top1",
        "hyperparameters": {
            "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "additionalProperties": False,
            "properties": {"epochs": {"type": "integer", "minimum": 1, "maximum": 200}, "batch_size": {"type": "integer"},
                           "image_size": {"type": "integer"}}},
        "presets": [{
            "name": "small", "base_model": {"repo_id": "timm/convnext_nano", "revision": REVISION}, "learning_rate": 1e-4,
            "preprocessing": IMAGENET, "cpu": 4, "memory_bytes": 1 << 30, "gpu": 1, "vram_gb": 8, "max_runtime_s": 600,
            "hyperparameters": LIMITS}],
        "requires": ["torch", "timm"],
        **over,
    }


def bundle_zip(doc: dict | None = None, sources: dict[str, bytes] | None = None, extra: dict[str, bytes] | None = None) -> bytes:
    files = {MANIFEST_FILE: json.dumps(manifest() if doc is None else doc).encode(), **(SOURCES if sources is None else sources)}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, data in {**{f"{ZIP_ROOT}/{p}": d for p, d in files.items()}, **(extra or {})}.items():
            zf.writestr(zipfile.ZipInfo(path, ZIP_TIMESTAMP), data)
    return buf.getvalue()
