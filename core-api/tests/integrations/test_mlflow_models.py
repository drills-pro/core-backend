"""Models live in MLflow: trainers upload into the run core-api created, serving downloads from it,
and MLflow names are namespaced so tenants cannot reach each other's experiments or models."""
import asyncio
import base64
import re

import httpx
import pytest

from app.integrations import compute, serve_client
from app.integrations.data_plane import access, mlflow

T, P = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
ARTIFACT_PATH = "7/abc123/artifacts"


def _script_of(entrypoint: str) -> str:
    return base64.b64decode(re.search(r"b64decode\('([^']+)'", entrypoint).group(1)).decode()


@pytest.fixture
def scripts(monkeypatch):
    seen = []

    async def submit(spec):
        seen.append(_script_of(spec.entrypoint))
        return "job-1"

    monkeypatch.setattr(compute, "submit", submit)
    return seen


def test_serving_downloads_from_the_mlflow_artifact_api(scripts):
    weights = {"onnx_key": f"{ARTIFACT_PATH}/model/model.onnx", "class_names": ["a", "b"]}
    asyncio.run(serve_client.deploy(deployment_id="d", weights=weights, tenant_id=T, project_id=P))
    script = scripts[0]
    compile(script, "serve", "exec")
    assert f"{mlflow.artifact_url('')}{ARTIFACT_PATH}/model/model.onnx" in script
    assert "boto3" not in script


def test_names_are_namespaced_per_tenant_and_project():
    other = "33333333-3333-3333-3333-333333333333"
    assert mlflow.experiment_name(T, P) != mlflow.experiment_name(other, P)
    full = mlflow.registered_model_name(T, P, "classifier")
    assert full != mlflow.registered_model_name(other, P, "classifier")
    assert mlflow.display_name(full, T, P) == "classifier"
    assert mlflow.display_name("legacy-name", T, P) == "legacy-name"


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


D, V = "44444444-4444-4444-4444-444444444444", "55555555-5555-5555-5555-555555555555"


@pytest.fixture(autouse=True)
def _owned(monkeypatch):
    async def ok(*_):
        return "commit"

    monkeypatch.setattr(access, "project", ok)
    monkeypatch.setattr(access, "version_commit", ok)


def test_start_run_creates_a_namespaced_experiment_and_returns_the_artifact_path(monkeypatch):
    calls = _mlflow(monkeypatch, "mlflow-artifacts:/7/abc123/artifacts")
    run = asyncio.run(mlflow.start_run(T, P, D, V))
    assert (run.run_id, run.artifact_path) == ("abc123", ARTIFACT_PATH)
    created = next(c for c in calls if c.url.path.endswith("experiments/create"))
    assert mlflow.experiment_name(T, P) in created.content.decode()


def test_start_run_refuses_an_mlflow_that_does_not_serve_artifacts(monkeypatch):
    _mlflow(monkeypatch, "s3://bucket/7/abc123/artifacts")
    with pytest.raises(RuntimeError, match="not serving artifacts"):
        asyncio.run(mlflow.start_run(T, P, D, V))
