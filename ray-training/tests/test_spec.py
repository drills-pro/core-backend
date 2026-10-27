import json
from pathlib import Path

import jsonschema
import pytest

from ray_training.constants import (
    DEFAULT_BATCH_SIZE, DEFAULT_EPOCHS, DEFAULT_IMAGE_SIZE, FailCode, Outcome, Preset, Task)
from ray_training.presets import PRESETS
from ray_training.spec import parse_spec
from tests.support import VERSION_ID, classification_rows, spec_doc

ROWS = classification_rows()[0]
REPO = Path(__file__).resolve().parents[2]
CONTRACT = REPO / "contracts" / "job-spec.v1.json"
REPORT_SCHEMA_FILE = REPO / "core-api/app/integrations/data_plane/train_report.schema.json"


def test_payload_fields_and_preset_defaults():
    doc = spec_doc("image-classification", "m", ROWS)
    del doc["payload"]["hyperparameters"]
    spec = parse_spec(json.dumps(doc))
    d = PRESETS[spec.task][spec.preset]
    assert (spec.epochs, spec.batch_size, spec.image_size, spec.learning_rate) == (30, 16, 224, d.learning_rate)
    assert (spec.bucket, spec.version_id, spec.lakefs_commit, spec.seed, spec.base_model) == ("b", VERSION_ID, "c0ffee", 42, "m")


def test_base_model_defaults_to_the_preset_when_absent():
    doc = spec_doc("object-detection", "m", ROWS)
    del doc["payload"]["base_model"]
    assert parse_spec(json.dumps(doc)).base_model == PRESETS["object-detection"]["small"].base_model


@pytest.mark.parametrize("over", [{"spec_version": 2}, {"kind": "ingest"}])
def test_an_unknown_version_or_kind_is_refused(over):
    with pytest.raises(ValueError):
        parse_spec(json.dumps({**spec_doc("image-classification", "m", ROWS), **over}))


@pytest.mark.parametrize("field,value", [("preset", "huge"), ("task", "image-segmentation")])
def test_a_bad_preset_or_task_is_refused(field, value):
    doc = spec_doc("image-classification", "m", ROWS)
    doc["payload"][field] = value
    with pytest.raises(ValueError):
        parse_spec(json.dumps(doc))


def test_a_spec_validates_against_the_committed_contract():
    """contracts/job-spec.v1.json is generated from core-api's TrainSpec: the trainer reads what core-api writes."""
    schema = json.loads(CONTRACT.read_text())
    jsonschema.validate(spec_doc("image-classification", "m", ROWS), schema)
    jsonschema.validate(spec_doc("object-detection", "m", ROWS), schema)


def test_the_contract_enums_equal_the_trainers():
    defs = json.loads(CONTRACT.read_text())["$defs"]
    assert set(defs["TrainPreset"]["enum"]) == {p.value for p in Preset}
    assert set(defs["TrainPayload"]["properties"]["task"]["enum"]) == {t.value for t in Task}
    hp = defs["Hyperparameters"]["properties"]
    assert (hp["epochs"]["default"], hp["batch_size"]["default"], hp["image_size"]["default"]) == (
        DEFAULT_EPOCHS, DEFAULT_BATCH_SIZE, DEFAULT_IMAGE_SIZE)


@pytest.mark.skipif(not REPORT_SCHEMA_FILE.exists(), reason="core-api is not next to this package")
def test_report_vocabulary_equals_core_apis_schema():
    defs = json.loads(REPORT_SCHEMA_FILE.read_text())["$defs"]
    assert set(defs["TrainOutcome"]["enum"]) == {o.value for o in Outcome}
    assert set(defs["FailureCode"]["enum"]) == {c.value for c in FailCode}
