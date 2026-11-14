"""The ingest spec of an import that has a base version or comes from Hugging Face."""
import asyncio
import uuid

import pytest

from app.core.enums import ImportSource, TaskType
from app.core.settings import settings
from app.core.task_catalog import LABEL_COLUMN, DataKind
from app.integrations.data_plane import layout
from app.modules.user.datasets import ingest_spec

TENANT, DATASET, IMPORT, BASE = (str(uuid.uuid4()) for _ in range(4))


@pytest.fixture
def presigned(monkeypatch):
    asked = []

    async def presign_get(tenant_id, dataset_id, version_id, key):
        asked.append((tenant_id, dataset_id, version_id, key))
        return f"http://s3/{version_id}/{key}"

    monkeypatch.setattr(ingest_spec.data_plane, "presign_get", presign_get)
    return asked


def build(task, kind, source=ImportSource.UPLOAD, params=None, base=None):
    row = {"tenant_id": TENANT, "dataset_id": DATASET, "id": IMPORT, "source": source, "params": params or {}, "base_version_id": base}
    return asyncio.run(ingest_spec.build(row, task, kind, settings.dataset_limits, "ingest-1"))


def test_an_image_import_on_a_base_version_reads_only_that_versions_index(presigned):
    spec, _ = build(TaskType.IMAGE_CLASSIFICATION, DataKind.IMAGES, base=BASE)
    assert [(a[2], a[3]) for a in presigned] == [(BASE, ingest_spec.DATA_KIND_LAYOUTS[DataKind.IMAGES].data_key)]
    assert spec.payload.base.index_url.startswith(f"http://s3/{BASE}/") and spec.payload.base.data_url is None


def test_a_table_import_on_a_base_version_reads_its_data_and_schema(presigned):
    spec, _ = build(TaskType.TABULAR_REGRESSION, DataKind.TABULAR, base=BASE)
    assert [a[3] for a in presigned] == [ingest_spec.DATA_KIND_LAYOUTS[DataKind.TABULAR].data_key, layout.SCHEMA_KEY]
    assert spec.payload.base.data_url and spec.payload.base.schema_url and spec.payload.base.index_url is None


def test_a_huggingface_import_names_the_repo_and_defaults_the_revision():
    spec, _ = build(TaskType.TABULAR_CLASSIFICATION, DataKind.TABULAR, ImportSource.HUGGINGFACE, {"repo": "org/ds", "column": "c", "target": "y"})
    assert (spec.payload.hf.repo, spec.payload.hf.revision, spec.payload.hf.column) == ("org/ds", ingest_spec.HF_DEFAULT_REVISION, "c")
    pinned, _ = build(TaskType.TABULAR_CLASSIFICATION, DataKind.TABULAR, ImportSource.HUGGINGFACE, {"repo": "org/ds", "revision": "abc"})
    assert pinned.payload.hf.revision == "abc"


def test_a_text_import_always_targets_the_label_column():
    spec, _ = build(TaskType.TEXT_CLASSIFICATION, DataKind.TEXT, params={"target": "ignored"})
    assert spec.payload.target == LABEL_COLUMN
