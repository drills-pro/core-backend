"""train: a table dataset needs only its data file, and model.json is read back as the run left it."""
import asyncio
import io
import json
import uuid

import pytest
from botocore.exceptions import ClientError

from app.core.settings import settings
from app.core.task_catalog import DataKind
from app.integrations.data_plane import access, layout, train
from tests.support import FakeConn, TenantCtx

TENANT, PROJECT, DATASET, VERSION, RUN = (str(uuid.uuid4()) for _ in range(5))
COMMIT = "c0ffee"
PREFIX = layout.train_prefix(TENANT, PROJECT, RUN)


@pytest.fixture(autouse=True)
def owned(monkeypatch):
    monkeypatch.setattr(access, "TenantConn", lambda tenant_id: TenantCtx(FakeConn(val=1, vals={"lakefs_commit": COMMIT})))


@pytest.mark.parametrize("kind", [DataKind.TABULAR, DataKind.TEXT])
def test_a_table_dataset_presigns_its_data_file_and_stages_no_manifest(monkeypatch, kind):
    seen = {}

    async def presign(tenant_id, dataset_id, version_id, keys, ttl):
        seen["keys"], seen["ttl"] = keys, ttl
        return {k: f"http://data/{k}" for k in keys}

    monkeypatch.setattr(train.lakefs, "presign_version_objects", presign)
    monkeypatch.setattr(train.lakefs, "s3_client", lambda secret: pytest.fail("a manifest was staged"))
    data = asyncio.run(train.prepare_train_data(TENANT, PROJECT, DATASET, VERSION, RUN, 77, data_kind=kind))
    assert seen == {"keys": [layout.DATA_KEY], "ttl": 77}
    assert (data.data_url, data.index_url, data.manifest_url) == (f"http://data/{layout.DATA_KEY}", None, None)
    assert (data.lakefs_commit, str(data.version_id)) == (COMMIT, VERSION)


class S3:
    def __init__(self, objects=None, error="NoSuchKey"):
        self.objects, self.error = objects or {}, error

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": self.error}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}


def test_the_model_meta_is_the_runs_model_json(monkeypatch):
    meta = {"framework": "onnx", "classes": ["a", "b"]}
    monkeypatch.setattr(train.lakefs, "s3_client", lambda secret: S3({PREFIX + layout.MODEL_META_KEY: json.dumps(meta).encode()}))
    assert asyncio.run(train.read_model_meta(TENANT, PROJECT, RUN)) == meta


def test_a_run_without_a_model_json_has_no_meta(monkeypatch):
    monkeypatch.setattr(train.lakefs, "s3_client", lambda secret: S3())
    assert asyncio.run(train.read_model_meta(TENANT, PROJECT, RUN)) is None


def test_any_other_store_error_reading_the_meta_is_raised(monkeypatch):
    monkeypatch.setattr(train.lakefs, "s3_client", lambda secret: S3(error="AccessDenied"))
    with pytest.raises(ClientError):
        asyncio.run(train.read_model_meta(TENANT, PROJECT, RUN))


def test_the_staging_bucket_is_where_the_meta_is_read(monkeypatch):
    asked = {}

    class Spy(S3):
        def get_object(self, Bucket, Key):
            asked.update(bucket=Bucket, key=Key)
            return {"Body": io.BytesIO(b"{}")}

    monkeypatch.setattr(train.lakefs, "s3_client", lambda secret: Spy())
    asyncio.run(train.read_model_meta(TENANT, PROJECT, RUN))
    assert asked == {"bucket": settings.data_plane.staging_bucket, "key": PREFIX + layout.MODEL_META_KEY}
