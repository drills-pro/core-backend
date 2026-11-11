"""The job spec: one envelope, a payload per kind, and the committed JSON Schema matches the models."""
import json
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.enums import ImportSource, TaskType
from app.core.settings import settings
from app.integrations.job_spec import (
    SPEC_ADAPTER,
    SPEC_VERSION,
    BatchPredictSpec,
    Hyperparameters,
    IngestPayload,
    IngestSpec,
    JobKind,
    Resources,
    TrainSpec,
)

ROOT = Path(__file__).parents[3]
SCHEMA = ROOT / "contracts" / "job-spec.v1.json"
MIGRATIONS = Path(__file__).parents[2] / "migrations"
RESOURCES = {"cpu": 2, "memory_bytes": 1024}
TRAIN_RESOURCES = {**RESOURCES, "gpu": 1, "max_runtime_s": 60}


def envelope(kind, payload, **over):
    return {"spec_version": SPEC_VERSION, "kind": kind, "job_id": "j", "tenant_id": str(uuid.uuid4()), "resources": RESOURCES,
            "payload": payload, **over}


def ingest_payload():
    return IngestPayload(bucket="b", prefix="p/", source=ImportSource.UPLOAD, task=TaskType.TABULAR_REGRESSION,
                         target="y", limits=settings.dataset_limits).model_dump(mode="json")


def train_payload(**over):
    return {"bucket": "b", "prefix": "trainings/t/p/r/", "task": TaskType.IMAGE_CLASSIFICATION, "trainer": "ray_training.classification:train", "data_kind": "images", "preset": "small",
            "base_model": {"repo_id": "facebook/convnext-tiny-224", "revision": "abc"}, "learning_rate": 1e-4,
            "preprocessing": {"mean": [0.5, 0.5, 0.5], "std": [0.5, 0.5, 0.5], "rescale": 1 / 255, "resample": "bilinear"},
            "data": {"index_url": "http://i", "manifest_url": "http://m", "version_id": str(uuid.uuid4()), "lakefs_commit": "c0ffee"},
            "hyperparameters": {}, "seed": 1, **over}


def train_spec(**over):
    return envelope(JobKind.TRAIN, train_payload(**over.pop("payload", {})), resources=TRAIN_RESOURCES, **over)


def batch_spec(**over):
    payload = {"bucket": "b", "prefix": "batch/t/p/", "data": {"index_url": "http://i", "manifest_url": "http://m", "version_id": str(uuid.uuid4()), "lakefs_commit": "c0ffee"},
               "model": {"model_url": "http://x/model.onnx", "manifest_url": "http://x/model.json", "tokenizer_url": "", "class_names": ["a", "b"], "params": {},
                         "providers": ["CPUExecutionProvider"], "encoder": "image_encoder", "postprocess": "classify"}, **over}
    return envelope(JobKind.PREDICT_BATCH, payload)


def test_the_committed_schema_matches_the_models():
    current = json.dumps(SPEC_ADAPTER.json_schema(), indent=2, sort_keys=True) + "\n"
    assert SCHEMA.read_text() == current, "regenerate: cd core-api && APP_ENV=test PYTHONPATH=. .venv/bin/python ../scripts/gen-job-spec-schema.py"


def test_the_kind_picks_the_payload():
    assert isinstance(SPEC_ADAPTER.validate_python(envelope(JobKind.INGEST, ingest_payload())), IngestSpec)
    assert isinstance(SPEC_ADAPTER.validate_python(train_spec()), TrainSpec)


def test_an_unknown_version_or_kind_is_rejected():
    with pytest.raises(ValidationError):
        SPEC_ADAPTER.validate_python(envelope(JobKind.INGEST, ingest_payload(), spec_version=2))
    with pytest.raises(ValidationError):
        SPEC_ADAPTER.validate_python(train_spec(spec_version=2))
    with pytest.raises(ValidationError):
        SPEC_ADAPTER.validate_python(envelope("prepare", {}))


def test_a_train_spec_round_trips_with_its_defaults_filled():
    spec = TrainSpec.model_validate(train_spec())
    assert spec.payload.hyperparameters == Hyperparameters() and spec.resources.gpu == 1
    assert TrainSpec.model_validate_json(spec.model_dump_json()) == spec


@pytest.mark.parametrize("bad", [
    {"trainer": "not a trainer"},
    {"data_kind": "text"},  # a table task needs data_url
    {"hyperparameters": {"epochs": 0}},
    {"hyperparameters": {"image_size": 4}},
    {"data": {"index_url": "http://i"}},
    {"learning_rate": 0},
    {"preprocessing": {"mean": [0.5, 0.5, 0.5]}},
    {"preprocessing": {"mean": [0.5], "std": [0.5, 0.5, 0.5], "rescale": 1 / 255, "resample": "bilinear"}},
    {"preprocessing": {"mean": [0.5] * 3, "std": [0.5] * 3, "rescale": 1 / 255, "resample": "lanczos"}},
])
def test_a_bad_train_payload_is_rejected(bad):
    with pytest.raises(ValidationError):
        SPEC_ADAPTER.validate_python(train_spec(payload=bad))


def test_resources_must_be_declared_and_positive():
    with pytest.raises(ValidationError):
        Resources(cpu=0, memory_bytes=1)


def test_a_predict_batch_spec_is_picked_by_its_kind_and_fills_the_knobs():
    spec = SPEC_ADAPTER.validate_python(batch_spec())
    assert isinstance(spec, BatchPredictSpec) and (spec.payload.concurrency, spec.payload.batch_size) == (2, 16)
    assert BatchPredictSpec.model_validate_json(spec.model_dump_json()) == spec


@pytest.mark.parametrize("bad", [
    {"concurrency": 0},
    {"concurrency": 33},
    {"batch_size": 0},
    {"batch_size": 257},
    {"data": {"index_url": "http://i", "version_id": str(uuid.uuid4()), "lakefs_commit": "c"}},  # no manifest_url
    {"data": {"index_url": "", "manifest_url": "http://m", "version_id": str(uuid.uuid4()), "lakefs_commit": "c"}},
    {"model": {"manifest_url": "http://x/model.json"}},  # no model_url
])
def test_a_bad_predict_batch_payload_is_rejected(bad):
    with pytest.raises(ValidationError):
        SPEC_ADAPTER.validate_python(batch_spec(**bad))
