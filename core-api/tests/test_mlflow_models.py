"""Models live in MLflow: trainers upload into the run core-api created, serving downloads from it,
and MLflow names are namespaced so tenants cannot reach each other's experiments or models."""
import asyncio
import base64
import re

import httpx
import pytest

from app.core import secret_files
from app.integrations import mlflow_client, ray_client, serve_client

T, P = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
ARTIFACT_PATH = "7/abc123/artifacts"
LAKEFS = {"endpoint": "http://lakefs:8000", "access_key": "LK", "secret_key": "LS"}


def _script_of(entrypoint: str) -> str:
    return base64.b64decode(re.search(r"b64decode\('([^']+)'", entrypoint).group(1)).decode()


@pytest.fixture
def scripts(monkeypatch):
    monkeypatch.setattr(secret_files, "get", lambda name: LAKEFS)
    seen = []

    async def submit(*, entrypoint, **_):
        seen.append(_script_of(entrypoint))
        return "job-1"

    monkeypatch.setattr(ray_client, "submit_raw_job", submit)
    return seen


def test_trainer_uploads_the_model_into_the_mlflow_run(scripts):
    asyncio.run(
        ray_client.submit_job(
            tenant_id=T,
            project_id=P,
            dataset_ref="r",
            config={"backend": "ultralytics"},
            file_keys=["a/b.jpg"],
            mlflow_artifact_path=ARTIFACT_PATH,
        )
    )
    script = scripts[0]
    compile(script, "train", "exec")
    assert mlflow_client.ARTIFACTS_API in script
    assert f"{ARTIFACT_PATH}/model/model.onnx" not in script  # built at runtime from the path + file name
    assert f"'{ARTIFACT_PATH}/model' + \"/model.onnx\"" in script
    assert "boto3 as _boto3" not in script and "s3_upload" not in script


def test_ultralytics_needs_a_run_to_upload_into():
    with pytest.raises(ValueError, match="artifact path"):
        ray_client._build_entrypoint({"backend": "ultralytics"}, "r", ["a/b.jpg"])


def test_serving_downloads_from_the_mlflow_artifact_api(scripts):
    weights = {"onnx_key": f"{ARTIFACT_PATH}/model/model.onnx", "class_names": ["a", "b"]}
    asyncio.run(serve_client.deploy(deployment_id="d", weights=weights, tenant_id=T, project_id=P))
    script = scripts[0]
    compile(script, "serve", "exec")
    assert f"{mlflow_client.ARTIFACTS_API}/{ARTIFACT_PATH}/model/model.onnx" in script
    assert "boto3" not in script


def test_names_are_namespaced_per_tenant_and_project():
    other = "33333333-3333-3333-3333-333333333333"
    assert mlflow_client.experiment_name(T, P) != mlflow_client.experiment_name(other, P)
    full = mlflow_client.registered_model_name(T, P, "classifier")
    assert full != mlflow_client.registered_model_name(other, P, "classifier")
    assert mlflow_client.display_name(full, T, P) == "classifier"
    assert mlflow_client.display_name("legacy-name", T, P) == "legacy-name"


def _mlflow(monkeypatch, artifact_uri):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path.endswith("experiments/get-by-name"):
            return httpx.Response(404)
        if path.endswith("experiments/create"):
            return httpx.Response(200, json={"experiment_id": "7"})
        return httpx.Response(200, json={"run": {"info": {"run_id": "abc123", "artifact_uri": artifact_uri}}})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return calls


def test_start_run_creates_a_namespaced_experiment_and_returns_the_artifact_path(monkeypatch):
    calls = _mlflow(monkeypatch, "mlflow-artifacts:/7/abc123/artifacts")
    run_id, path = asyncio.run(mlflow_client.start_run(tenant_id=T, project_id=P, dataset_ref="r"))
    assert (run_id, path) == ("abc123", ARTIFACT_PATH)
    created = next(c for c in calls if c.url.path.endswith("experiments/create"))
    assert mlflow_client.experiment_name(T, P) in created.content.decode()


def test_start_run_refuses_an_mlflow_that_does_not_serve_artifacts(monkeypatch):
    _mlflow(monkeypatch, "s3://bucket/7/abc123/artifacts")
    with pytest.raises(RuntimeError, match="not serving artifacts"):
        asyncio.run(mlflow_client.start_run(tenant_id=T, project_id=P, dataset_ref="r"))
