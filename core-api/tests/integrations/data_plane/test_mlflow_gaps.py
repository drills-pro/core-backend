"""mlflow: health, experiment reuse and the create race, run ownership, metric logging, model publish failures and registry errors."""
import asyncio
import io
import json
import uuid

import httpx
import pytest
from botocore.exceptions import ClientError

from app.core.problem import Problem
from app.integrations.data_plane import access, lakefs, layout, mlflow, prediction_payloads
from tests.support import FakeConn, TenantCtx

T, P, D, V, RUN = (str(uuid.uuid4()) for _ in range(5))
OWN_TAGS = [{"key": mlflow.TAG_TENANT, "value": T}, {"key": mlflow.TAG_PROJECT, "value": P}]


@pytest.fixture(autouse=True)
def owned(monkeypatch):
    async def ok(*_):
        return "commit"

    monkeypatch.setattr(access, "project", ok)
    monkeypatch.setattr(access, "version_commit", ok)
    monkeypatch.setattr(access, "TenantConn", lambda tenant_id: TenantCtx(FakeConn(val=1)))


def _body(request):
    try:
        return json.loads(request.content or b"null")
    except ValueError:  # an artifact upload is raw bytes
        return None


def stub(monkeypatch, handler):
    seen = []

    def wrapped(request):
        seen.append((request.method, request.url.path.split("/api/2.0/mlflow/")[-1], _body(request)))
        return handler(request)

    real = httpx.AsyncClient
    monkeypatch.setattr(mlflow.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(wrapped), **kw))
    return seen


def run_json(tags=OWN_TAGS, uri="mlflow-artifacts:/1/r/artifacts"):
    return {"run": {"info": {"run_id": "r", "artifact_uri": uri}, "data": {"tags": tags}}}


def test_health_is_ok_when_mlflow_answers(monkeypatch):
    seen = stub(monkeypatch, lambda r: httpx.Response(200))
    assert asyncio.run(mlflow.health()) == layout.HEALTH_OK
    assert seen[0][0] == "GET"


def test_health_reports_the_failure_instead_of_raising(monkeypatch):
    stub(monkeypatch, lambda r: httpx.Response(503))
    assert asyncio.run(mlflow.health()).startswith(layout.HEALTH_ERROR)


def experiments(get_statuses, create_status=200):
    gets = iter(get_statuses)

    def handler(request):
        path = request.url.path
        if path.endswith("experiments/get-by-name"):
            status = next(gets)
            return httpx.Response(status, json={"experiment": {"experiment_id": "42"}} if status == 200 else {})
        if path.endswith("experiments/create"):
            return httpx.Response(create_status, json={"experiment_id": "7"} if create_status == 200 else {})
        return httpx.Response(200, json=run_json())

    return handler


def test_an_existing_experiment_is_reused_not_recreated(monkeypatch):
    seen = stub(monkeypatch, experiments([200]))
    asyncio.run(mlflow.start_run(T, P, D, V))
    assert [path for _, path, _ in seen] == ["experiments/get-by-name", "runs/create"]
    assert seen[1][2]["experiment_id"] == "42"


def test_losing_the_experiment_create_race_reads_the_winners_experiment(monkeypatch):
    seen = stub(monkeypatch, experiments([404, 200], create_status=400))
    asyncio.run(mlflow.start_run(T, P, D, V))
    assert [path for _, path, _ in seen] == ["experiments/get-by-name", "experiments/create", "experiments/get-by-name", "runs/create"]
    assert seen[-1][2]["experiment_id"] == "42"


def test_a_failed_experiment_create_raises(monkeypatch):
    stub(monkeypatch, experiments([404], create_status=500))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(mlflow.start_run(T, P, D, V))


def test_a_run_started_is_tagged_with_its_tenant_project_and_dataset_version(monkeypatch):
    seen = stub(monkeypatch, experiments([200]))
    asyncio.run(mlflow.start_run(T, P, D, V))
    tags = {t["key"]: t["value"] for t in seen[-1][2]["tags"]}
    assert tags == {mlflow.TAG_TENANT: T, mlflow.TAG_PROJECT: P, mlflow.TAG_DATASET_VERSION: V}


def test_finishing_a_run_logs_its_metrics_then_closes_it(monkeypatch):
    def handler(request):
        return httpx.Response(200, json=run_json())

    seen = stub(monkeypatch, handler)
    asyncio.run(mlflow.finish_run(T, P, "r", mlflow.RunStatus.FINISHED, {"top1": 0.9}))
    assert [path for _, path, _ in seen] == ["runs/get", "runs/log-batch", "runs/update"]
    assert [(m["key"], m["value"]) for m in seen[1][2]["metrics"]] == [("top1", 0.9)]
    assert (seen[2][2]["run_id"], seen[2][2]["status"]) == ("r", "FINISHED")


def test_finishing_without_metrics_only_closes_the_run(monkeypatch):
    seen = stub(monkeypatch, lambda r: httpx.Response(200, json=run_json()))
    asyncio.run(mlflow.finish_run(T, P, "r", mlflow.RunStatus.FAILED))
    assert [path for _, path, _ in seen] == ["runs/get", "runs/update"]


@pytest.mark.parametrize("answer", [
    httpx.Response(404, json={}),
    httpx.Response(200, json=run_json(tags=[{"key": mlflow.TAG_TENANT, "value": str(uuid.uuid4())}, OWN_TAGS[1]])),  # another tenant's run
    httpx.Response(200, json=run_json(tags=[])),
])
def test_a_missing_or_foreign_run_is_a_404_and_nothing_is_written(monkeypatch, answer):
    seen = stub(monkeypatch, lambda r: answer)
    with pytest.raises(Problem) as err:
        asyncio.run(mlflow.finish_run(T, P, "r", mlflow.RunStatus.FINISHED, {"top1": 1.0}))
    assert err.value.status_code == 404
    assert [path for _, path, _ in seen] == ["runs/get"]


class Store:
    def __init__(self, objects, error="NoSuchKey"):
        self.objects, self.error = objects, error

    def get_object(self, Bucket, Key):
        name = Key.rsplit("/", 1)[-1]
        if name not in self.objects:
            raise ClientError({"Error": {"Code": self.error}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[name]), "ContentLength": len(self.objects[name])}


def publish(monkeypatch, store):
    puts = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=run_json())
        puts.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200)

    stub(monkeypatch, handler)
    monkeypatch.setattr(mlflow.lakefs, "s3_client", lambda secret: store)
    return puts


def test_a_text_models_tokenizer_is_published_with_it(monkeypatch):
    puts = publish(monkeypatch, Store({layout.MODEL_KEY: b"m", layout.MODEL_META_KEY: b"{}", layout.TOKENIZER_KEY: b"tok"}))
    asyncio.run(mlflow.publish_run_model(T, P, RUN, "r"))
    assert puts == [layout.MODEL_KEY, layout.MODEL_META_KEY, layout.TOKENIZER_KEY]


@pytest.mark.parametrize("missing", [layout.MODEL_KEY, layout.MODEL_META_KEY])
def test_a_run_missing_its_model_or_manifest_fails_loudly_instead_of_publishing_half(monkeypatch, missing):
    files = {layout.MODEL_KEY: b"m", layout.MODEL_META_KEY: b"{}"}
    del files[missing]
    publish(monkeypatch, Store(files))
    with pytest.raises(ClientError):
        asyncio.run(mlflow.publish_run_model(T, P, RUN, "r"))


def test_a_rejected_artifact_upload_raises(monkeypatch):
    stub(monkeypatch, lambda r: httpx.Response(200, json=run_json()) if r.method == "GET" else httpx.Response(500))
    monkeypatch.setattr(mlflow.lakefs, "s3_client", lambda secret: Store({layout.MODEL_KEY: b"m", layout.MODEL_META_KEY: b"{}"}))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(mlflow.publish_run_model(T, P, RUN, "r"))


def registry(monkeypatch, create_status):
    def handler(request):
        path = request.url.path
        if path.endswith("registered-models/create"):
            return httpx.Response(create_status, json={})
        if path.endswith("model-versions/search"):
            return httpx.Response(200, json={})
        if path.endswith("model-versions/create"):
            return httpx.Response(200, json={"model_version": {"version": "3"}})
        return httpx.Response(200, json=run_json())

    return stub(monkeypatch, handler)


def test_an_already_registered_model_name_is_fine(monkeypatch):
    seen = registry(monkeypatch, 400)
    assert asyncio.run(mlflow.register_model_version(T, P, "clf", "r")) == "3"
    created = next(body for _, path, body in seen if path == "model-versions/create")
    assert created["name"] == mlflow.registered_model_name(T, P, "clf") and created["source"] == "runs:/r/model"


def test_any_other_registry_failure_raises(monkeypatch):
    registry(monkeypatch, 500)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(mlflow.register_model_version(T, P, "clf", "r"))


def test_a_stage_transition_names_the_namespaced_model_and_keeps_other_versions(monkeypatch):
    version = {"name": mlflow.registered_model_name(T, P, "clf"), "version": "3", "current_stage": "Staging"}
    seen = stub(monkeypatch, lambda r: httpx.Response(200, json={"model_version": version}))
    got = asyncio.run(mlflow.transition_stage(T, P, "clf", "3", mlflow.ModelStage.STAGING))
    assert got == mlflow.ModelVersion(**version)
    assert seen[0][2] == {"name": version["name"], "version": "3", "stage": "Staging", "archive_existing_versions": False}


def test_a_refused_stage_transition_raises(monkeypatch):
    stub(monkeypatch, lambda r: httpx.Response(404, json={}))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(mlflow.transition_stage(T, P, "clf", "3", mlflow.ModelStage.PRODUCTION))


# --- prediction payloads: the real client factory and the non-missing error ---------------------------------------

def test_a_store_error_other_than_missing_is_not_reported_as_a_missing_payload(monkeypatch):
    class Denied:
        def get_object(self, **kw):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    monkeypatch.setattr(lakefs, "_s3", lambda secret, endpoint=None: Denied())
    monkeypatch.setattr(prediction_payloads.secret_files, "get", lambda name: {"endpoint": "http://s3", "access_key": "k", "secret_key": "s"})
    key = prediction_payloads._prefix(T, P, RUN) + "input.json"
    with pytest.raises(ClientError):
        asyncio.run(prediction_payloads.load(T, P, RUN, key))
