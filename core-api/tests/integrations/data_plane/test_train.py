"""The training side of the gateway: staging the data, submitting the driver, reading the report, publishing the model."""
import asyncio
import io
import json
import shlex
import uuid
from pathlib import Path

import httpx
import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError

from app.core.enums import JobPackageName
from app.core.settings import settings
from app.integrations import compute
from app.integrations.data_plane import access, job_catalog, lakefs, layout, mlflow, train
from app.integrations.data_plane.train_report import FailureCode, TrainOutcome, TrainReport
from app.integrations.job_spec import SPEC_ADAPTER, TrainSpec
from tests.support import FakeConn, TenantCtx

TENANT, PROJECT, RUN, DATASET, VERSION = (str(uuid.uuid4()) for _ in range(5))
COMMIT = "c0ffee"
PREFIX = layout.train_prefix(TENANT, PROJECT, RUN)
SHA = "a" * 64
CREDS = {"endpoint": "http://s3.test", "access_key": "k", "secret_key": "s"}


@pytest.fixture
def owned(monkeypatch):
    monkeypatch.setattr(access, "TenantConn", lambda tenant_id: TenantCtx(FakeConn(val=1, vals={"lakefs_commit": COMMIT})))
    monkeypatch.setattr(train.secret_files, "get", lambda name: CREDS)
    monkeypatch.setattr(lakefs.secret_files, "get", lambda name: CREDS)


class S3:
    def __init__(self, objects=None):
        self.objects, self.put = objects or {}, {}

    def put_object(self, **kw):
        self.put[kw["Key"]] = kw["Body"]

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key]), "ContentLength": len(self.objects[Key])}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"http://ray-side/{Params['Bucket']}/{Params['Key']}?ttl={ExpiresIn}"


def test_the_report_schema_is_committed():
    committed = Path(train.__file__).with_name("train_report.schema.json").read_text()
    assert committed == json.dumps(TrainReport.model_json_schema(), indent=2) + "\n", "regenerate: python -m app.integrations.data_plane.train_report"


def test_a_report_has_a_failure_code_exactly_when_it_failed():
    TrainReport(status=TrainOutcome.SUCCEEDED, metrics={"top1": 0.9})
    TrainReport(status=TrainOutcome.FAILED, code=FailureCode.OOM)
    for bad in ({"status": "failed"}, {"status": "succeeded", "code": "oom"}, {"status": "succeeded", "metrics": {"top1": float("nan")}}):
        with pytest.raises(ValidationError):
            TrainReport.model_validate(bad)
    assert TrainReport.model_validate({"status": "succeeded", "something_new": 1}).status is TrainOutcome.SUCCEEDED  # unknown fields are ignored


def test_train_data_is_presigned_and_the_manifest_is_staged(owned, monkeypatch):
    s3 = S3()
    seen = {}

    async def column(*a):
        return ["cat/1.jpg", "dog/2.jpg"]

    async def presign(tenant_id, dataset_id, version_id, keys, ttl):
        seen["keys"], seen["ttl"] = keys, ttl
        return {k: f"http://img/{k}" for k in keys}

    monkeypatch.setattr(train.lakefs, "read_version_column", column)
    monkeypatch.setattr(train.lakefs, "presign_version_objects", presign)
    monkeypatch.setattr(train.lakefs, "s3_client", lambda secret: s3)
    data = asyncio.run(train.prepare_train_data(TENANT, PROJECT, DATASET, VERSION, RUN, 999))
    assert seen == {"keys": ["objects/cat/1.jpg", "objects/dog/2.jpg", layout.INDEX_KEY], "ttl": 999}
    assert json.loads(s3.put[PREFIX + layout.MANIFEST_KEY]) == {"cat/1.jpg": "http://img/objects/cat/1.jpg", "dog/2.jpg": "http://img/objects/dog/2.jpg"}
    assert (data.index_url, data.lakefs_commit, str(data.version_id)) == (f"http://img/{layout.INDEX_KEY}", COMMIT, VERSION)
    assert data.manifest_url == f"http://ray-side/{settings.data_plane.staging_bucket}/{PREFIX}{layout.MANIFEST_KEY}?ttl=999"


def test_presigned_objects_come_from_the_lakefs_gateway_at_the_commit(owned, monkeypatch):
    monkeypatch.setattr(lakefs, "_s3", lambda secret, endpoint=None: S3())
    urls = asyncio.run(lakefs.presign_version_objects(TENANT, DATASET, VERSION, ["objects/a.jpg"], 5))
    assert urls == {"objects/a.jpg": f"http://ray-side/{layout.repo_name(DATASET)}/{COMMIT}/objects/a.jpg?ttl=5"}


def test_the_driver_is_submitted_without_a_gpu_on_the_cpu_pool_with_the_staging_credential_only(owned, monkeypatch):
    from tests.integrations.test_job_spec import train_spec

    spec = TrainSpec.model_validate(train_spec())
    sent = {}

    async def submit(job):
        sent.update(job.model_dump())
        return "train-1"

    async def package_url(name, sha256):
        sent["package"] = (name, sha256)
        return "http://ray-side/jobs/ray-training-x.zip"

    monkeypatch.setattr(compute, "submit", submit)
    monkeypatch.setattr(train.job_package, "presigned_url", package_url)
    assert asyncio.run(train.submit_train(TENANT, PROJECT, RUN, spec, SHA)) == "train-1"
    argv = shlex.split(sent["entrypoint"])
    assert argv[3:5] == ["http://ray-side/jobs/ray-training-x.zip", job_catalog.TRAIN_ENTRYPOINT]
    assert SPEC_ADAPTER.validate_json(argv[5]) == spec  # the job reads exactly the spec core-api stored
    assert (sent["num_gpus"], sent["resources"], sent["submission_id"]) == (0, {compute.CPU_POOL_RESOURCE: 1}, spec.job_id)
    assert sent["timeout_seconds"] == spec.resources.max_runtime_s and sent["package"] == (JobPackageName.RAY_TRAINING, SHA)
    assert set(sent["env_vars"]) == {"STAGING_ENDPOINT", "STAGING_ACCESS_KEY", "STAGING_SECRET_KEY"}


def test_the_report_is_read_from_the_run_prefix(owned, monkeypatch):
    report = TrainReport(status=TrainOutcome.SUCCEEDED, metrics={"top1": 0.5})
    s3 = S3({PREFIX + layout.REPORT_KEY: report.model_dump_json().encode()})
    monkeypatch.setattr(train.lakefs, "s3_client", lambda secret: s3)
    assert asyncio.run(train.read_train_report(TENANT, PROJECT, RUN)) == report
    other = str(uuid.uuid4())
    assert asyncio.run(train.read_train_report(TENANT, PROJECT, other)) is None  # not written yet


def test_a_report_that_does_not_validate_raises(owned, monkeypatch):
    s3 = S3({PREFIX + layout.REPORT_KEY: b'{"status": "failed"}'})
    monkeypatch.setattr(train.lakefs, "s3_client", lambda secret: s3)
    with pytest.raises(ValidationError):
        asyncio.run(train.read_train_report(TENANT, PROJECT, RUN))


def test_the_model_files_are_copied_into_the_mlflow_run(owned, monkeypatch):
    s3 = S3({PREFIX + layout.MODEL_KEY: b"onnx-bytes", PREFIX + layout.MODEL_META_KEY: b"{}"})
    monkeypatch.setattr(mlflow.lakefs, "s3_client", lambda secret: s3)
    puts = {}

    def handler(request):
        if request.method == "GET":
            tags = [{"key": mlflow.TAG_TENANT, "value": TENANT}, {"key": mlflow.TAG_PROJECT, "value": PROJECT}]
            return httpx.Response(200, json={"run": {"data": {"tags": tags}, "info": {"artifact_uri": "mlflow-artifacts:/1/r/artifacts"}}})
        puts[request.url.path.split("/mlflow-artifacts/artifacts/", 1)[1]] = (request.content, request.headers["content-length"])
        return httpx.Response(200)

    real = httpx.AsyncClient
    monkeypatch.setattr(mlflow.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    asyncio.run(mlflow.publish_run_model(TENANT, PROJECT, RUN, "r"))
    assert puts == {"1/r/artifacts/model/model.onnx": (b"onnx-bytes", "10"), "1/r/artifacts/model/model.json": (b"{}", "2")}


def test_a_run_of_another_project_publishes_nothing(owned, monkeypatch):
    def handler(request):
        tags = [{"key": mlflow.TAG_TENANT, "value": TENANT}, {"key": mlflow.TAG_PROJECT, "value": str(uuid.uuid4())}]
        return httpx.Response(200, json={"run": {"data": {"tags": tags}, "info": {"artifact_uri": "mlflow-artifacts:/1/r/artifacts"}}})

    real = httpx.AsyncClient
    monkeypatch.setattr(mlflow.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(mlflow.lakefs, "s3_client", lambda secret: pytest.fail("the store was read"))
    with pytest.raises(Exception) as err:
        asyncio.run(mlflow.publish_run_model(TENANT, PROJECT, RUN, "r"))
    assert err.value.status_code == 404
