"""The bundle as the registry and the smoke job see it: manifest.json is valid, its entrypoints resolve, and the smoke spec trains
and exports in one process (the Ray run is the smoke job's own check). Same file in every bundle."""
import json
from importlib import metadata
from pathlib import Path

import jsonschema
import pytest

from ray_core.constants import CONTRACT, MODEL_KEY, REPORT_KEY
from ray_core.registry import load
from ray_core.runner import run
from ray_core.spec import parse_spec
from ray_core.store import http_get

MANIFEST = json.loads((Path(__file__).parents[1] / "manifest.json").read_text())


class MemStore:
    def __init__(self):
        self.puts = {}

    def put(self, key, data):
        self.puts[key] = data


def test_the_manifest_is_for_this_contract_and_its_entrypoints_resolve():
    assert MANIFEST["schema"] == 1 and MANIFEST["contract"] == CONTRACT
    assert MANIFEST["slug"] == Path(__file__).parents[1].name
    for target in [MANIFEST["trainer"], MANIFEST["smoke"]]:
        assert callable(load(target)), target


def test_the_hyperparameters_are_json_schema_and_every_preset_fits_them():
    jsonschema.Draft202012Validator.check_schema(MANIFEST["hyperparameters"])
    names = [p["name"] for p in MANIFEST["presets"]]
    assert names and len(names) == len(set(names))
    for preset in MANIFEST["presets"]:
        jsonschema.validate(preset["hyperparameters"], MANIFEST["hyperparameters"])


def test_the_smoke_spec_trains_and_exports_without_a_base_model():
    for dist in MANIFEST["requires"]:
        try:
            metadata.version(dist)
        except metadata.PackageNotFoundError:
            pytest.skip(f"{dist} is not installed here (the image has it; the smoke job checks)")
    doc = load(MANIFEST["smoke"])({"job_id": "j", "bucket": "b", "prefix": "p/"})
    spec = parse_spec(json.dumps(doc))
    assert (spec.task, spec.trainer.split(".")[0]) == (MANIFEST["slug"], MANIFEST["trainer"].split(".")[0])
    store = MemStore()
    report = run(spec, store, http_get)  # data: URLs are served by urlopen
    assert report["status"] == "succeeded", report["message"]
    assert report["history"] and MODEL_KEY in store.puts and REPORT_KEY in store.puts
    assert set(report["metrics"]) >= {MANIFEST["monitor"]}
