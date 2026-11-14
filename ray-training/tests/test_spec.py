import json
from pathlib import Path

import jsonschema
import pytest

from ray_core.constants import (
    DEFAULT_BATCH_SIZE, DEFAULT_EPOCHS, DEFAULT_IMAGE_SIZE, Augmentation, FailCode, LrSchedule, Outcome, Resample)
from ray_core.spec import parse_spec
from tests.support import LEARNING_RATE, PREPROCESSING, VERSION_ID, classification_rows, spec_doc

ROWS = classification_rows()[0]
REPO = Path(__file__).resolve().parents[2]
CONTRACT = REPO / "contracts" / "job-spec.v1.json"
REPORT_SCHEMA_FILE = REPO / "core-api/app/integrations/data_plane/train_report.schema.json"


def test_payload_fields_and_hyperparameter_defaults():
    doc = spec_doc("image-classification", "m", ROWS)
    del doc["payload"]["hyperparameters"]
    spec = parse_spec(json.dumps(doc))
    assert (spec.epochs, spec.batch_size, spec.image_size) == (30, 16, 224)
    assert (spec.bucket, spec.version_id, spec.lakefs_commit, spec.seed, spec.base_model) == ("b", VERSION_ID, "c0ffee", 42, "m")


def test_a_bundles_own_hyperparameters_and_an_empty_preprocessing_reach_the_trainer():
    doc = spec_doc("tabular-classification", "m", ROWS, preprocessing={})
    doc["payload"]["hyperparameters"] = {"target": "species", "n_estimators": 5}
    spec = parse_spec(json.dumps(doc))
    assert spec.hyperparameters == {"target": "species", "n_estimators": 5} and spec.preprocessing is None


def test_learning_rate_and_preprocessing_come_from_the_spec():
    spec = parse_spec(json.dumps(spec_doc("image-classification", "m", ROWS)))
    pre = spec.preprocessing
    assert spec.learning_rate == LEARNING_RATE
    assert (list(pre.mean), list(pre.std), pre.rescale, pre.resample.value) == (
        PREPROCESSING["mean"], PREPROCESSING["std"], PREPROCESSING["rescale"], PREPROCESSING["resample"])


@pytest.mark.parametrize("field", ["learning_rate", "preprocessing"])
def test_what_the_trainer_has_no_table_for_must_be_in_the_spec(field):
    doc = spec_doc("image-classification", "m", ROWS)
    del doc["payload"][field]
    with pytest.raises(KeyError):
        parse_spec(json.dumps(doc))


@pytest.mark.parametrize("base_model", [None, "absent"])
def test_a_preset_that_trains_from_scratch_has_no_base_model(base_model):
    doc = spec_doc("image-classification", "m", ROWS)
    if base_model is None:
        doc["payload"]["base_model"] = None
    else:
        del doc["payload"]["base_model"]
    spec = parse_spec(json.dumps(doc))
    assert (spec.base_model, spec.base_revision) == (None, None)


@pytest.mark.parametrize("over", [{"spec_version": 2}, {"kind": "ingest"}])
def test_an_unknown_version_or_kind_is_refused(over):
    with pytest.raises(ValueError):
        parse_spec(json.dumps({**spec_doc("image-classification", "m", ROWS), **over}))


def test_the_trainer_is_the_one_the_spec_names():
    doc = spec_doc("image-classification", "m", ROWS)
    doc["payload"]["trainer"] = "objdet.train:train"
    assert parse_spec(json.dumps(doc)).trainer == "objdet.train:train"


def test_a_spec_validates_against_the_committed_contract():
    """contracts/job-spec.v1.json is generated from core-api's TrainSpec: the trainer reads what core-api writes."""
    schema = json.loads(CONTRACT.read_text())
    jsonschema.validate(spec_doc("image-classification", "m", ROWS), schema)
    jsonschema.validate(spec_doc("object-detection", "m", ROWS), schema)


def test_the_contract_enums_equal_the_trainers():
    defs = json.loads(CONTRACT.read_text())["$defs"]
    assert set(defs["Resample"]["enum"]) == {r.value for r in Resample}
    assert set(defs["LrSchedule"]["enum"]) == {s.value for s in LrSchedule}
    assert set(defs["Augmentation"]["enum"]) == {a.value for a in Augmentation}
    hp = defs["Hyperparameters"]["properties"]
    assert (hp["epochs"]["default"], hp["batch_size"]["default"], hp["image_size"]["default"]) == (
        DEFAULT_EPOCHS, DEFAULT_BATCH_SIZE, DEFAULT_IMAGE_SIZE)


@pytest.mark.skipif(not REPORT_SCHEMA_FILE.exists(), reason="core-api is not next to this package")
def test_report_vocabulary_equals_core_apis_schema():
    defs = json.loads(REPORT_SCHEMA_FILE.read_text())["$defs"]
    assert set(defs["TrainOutcome"]["enum"]) == {o.value for o in Outcome}
    assert set(defs["FailureCode"]["enum"]) == {c.value for c in FailCode}
