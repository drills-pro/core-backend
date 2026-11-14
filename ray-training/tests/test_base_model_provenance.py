"""The base model's licence rides from the spec into model.json (each bundle checks it loads weights from safetensors only)."""
import json
from pathlib import Path

from ray_core.spec import parse_spec

from tests.support import classification_rows, spec_doc, spec_text

ROWS = classification_rows()[0]

SRC = Path(__file__).parent.parent / "ray_core"


def test_base_licence_comes_from_the_spec_and_is_written_to_the_manifest():
    base = {"repo_id": "m", "revision": "0" * 40, "license": "apache-2.0"}
    doc = spec_doc("image-classification", "m", ROWS)
    doc["payload"]["base_model"] = base
    assert parse_spec(json.dumps(doc)).base_license == "apache-2.0"
    assert parse_spec(spec_text("image-classification", "m", ROWS)).base_license is None
    assert '"base_license": spec.base_license' in (SRC / "runner.py").read_text()
