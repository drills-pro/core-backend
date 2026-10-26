"""The job spec: one envelope, a payload, and the committed JSON Schema matches the models."""
import json
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.enums import ImportSource, TaskType
from app.core.settings import settings
from app.integrations.job_spec import SPEC_VERSION, IngestSpec, IngestPayload, JobKind, Resources

SCHEMA = Path(__file__).parents[3] / "contracts" / "job-spec.v1.json"
RESOURCES = {"cpu": 2, "memory_bytes": 1024}


def envelope(kind, payload, **over):
    return {"spec_version": SPEC_VERSION, "kind": kind, "job_id": "j", "tenant_id": str(uuid.uuid4()), "resources": RESOURCES,
            "payload": payload, **over}


def ingest_payload():
    return IngestPayload(bucket="b", prefix="p/", source=ImportSource.UPLOAD, task=TaskType.TABULAR_REGRESSION,
                         target="y", limits=settings.dataset_limits).model_dump(mode="json")


def test_the_committed_schema_matches_the_models():
    current = json.dumps(IngestSpec.model_json_schema(), indent=2, sort_keys=True) + "\n"
    assert SCHEMA.read_text() == current, "regenerate: cd core-api && APP_ENV=test PYTHONPATH=. .venv/bin/python ../scripts/gen-job-spec-schema.py"


def test_the_payload_is_ingest():
    assert IngestSpec.model_validate(envelope(JobKind.INGEST, ingest_payload())).kind == JobKind.INGEST


def test_an_unknown_version_or_kind_is_rejected():
    with pytest.raises(ValidationError):
        IngestSpec.model_validate(envelope(JobKind.INGEST, ingest_payload(), spec_version=2))
    with pytest.raises(ValidationError):
        IngestSpec.model_validate(envelope("prepare", {}))


def test_resources_must_be_declared_and_positive():
    with pytest.raises(ValidationError):
        Resources(cpu=0, memory_bytes=1)
