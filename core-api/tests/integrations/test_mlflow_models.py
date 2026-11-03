"""Models live in MLflow: trainers upload into the run core-api created, serving downloads from it,
and MLflow names are namespaced so tenants cannot reach each other's experiments or models."""
import asyncio
import base64
import io
import re

import httpx
import pytest

from app.integrations import compute, serve_client
from tests.support.task_types import task_info
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


MODEL_URL = f"{mlflow.artifact_url('')}{ARTIFACT_PATH}/model/model.onnx"


@pytest.fixture
def model_url(monkeypatch):
    async def run_model_url(*_):
        return MODEL_URL

    monkeypatch.setattr(mlflow, "run_model_url", run_model_url)


def test_serving_downloads_from_the_mlflow_artifact_api(scripts, model_url):
    asyncio.run(serve_client.deploy(deployment_id="d", task=task_info(), mlflow_run_id="abc123", class_names=["a", "b"], tenant_id=T, project_id=P))
    script = scripts[0]
    compile(script, "serve", "exec")
    assert MODEL_URL in script and "['a', 'b']" in script
    assert MODEL_URL.replace("model.onnx", "model.json") in script  # preprocessing is read from the model's manifest
    assert "boto3" not in script and "(32, 32)" not in script


def test_serving_preprocesses_with_what_the_manifest_records(scripts, model_url):
    Image = pytest.importorskip("PIL.Image")
    np = pytest.importorskip("numpy")
    asyncio.run(serve_client.deploy(deployment_id="d", task=task_info(), mlflow_run_id="abc123", class_names=["a", "b"], tenant_id=T, project_id=P))
    assert "def image_encoder" in scripts[0]
    scope = {}
    exec(serve_client._source("serve_inputs.py"), scope)  # the part of the script that needs neither ray nor onnxruntime
    buf = io.BytesIO()
    Image.new("RGB", (5, 7), (51, 51, 51)).save(buf, "PNG")
    pre = {"resize": [4, 6], "resample": "bilinear", "rescale": 1 / 255, "mean": [0.5] * 3, "std": [0.25] * 3}
    feeds, width, height = scope["image_encoder"](pre, None, "pixel_values")({"image_base64": base64.b64encode(buf.getvalue()).decode()})
    arr = feeds["pixel_values"]
    assert (width, height) == (5, 7) and arr.shape == (1, 3, 6, 4) and np.allclose(arr, (0.2 - 0.5) / 0.25, atol=1e-5)  # NCHW, (height, width) swapped from "resize"


def test_the_model_url_is_under_the_runs_artifact_root(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        tags = [{"key": mlflow.TAG_TENANT, "value": T}, {"key": mlflow.TAG_PROJECT, "value": P}]
        run = {"info": {"artifact_uri": f"mlflow-artifacts:/{ARTIFACT_PATH}"}, "data": {"tags": tags}}
        return httpx.Response(200, json={"run": run})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    assert asyncio.run(mlflow.run_model_url(T, P, "abc123")) == MODEL_URL


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


def test_serve_url_follows_the_configured_ray_head(monkeypatch):
    from app.core.settings import settings
    monkeypatch.setattr(settings.ray, "dashboard", "http://ray-head-svc.ray.svc:8265")
    assert serve_client.serve_url() == f"http://ray-head-svc.ray.svc:{serve_client.SERVE_HTTP_PORT}"


def test_only_a_gpu_target_asks_for_a_gpu(monkeypatch, model_url):
    seen = []

    async def submit(spec):
        seen.append(spec.num_gpus)
        return "job-1"

    monkeypatch.setattr(compute, "submit", submit)
    for target in serve_client.ComputeTarget:
        asyncio.run(serve_client.deploy(
            deployment_id="d", task=task_info(), mlflow_run_id="abc123", class_names=["a"], tenant_id=T, project_id=P, compute_target=target,
        ))
    assert seen == [0, 1]


def _registry(monkeypatch, existing):
    created = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("model-versions/search"):
            assert "run_id='abc123'" in request.url.params["filter"]
            return httpx.Response(200, json={"model_versions": existing})
        if path.endswith("model-versions/create"):
            created.append(request)
            return httpx.Response(200, json={"model_version": {"version": "9"}})
        if path.endswith("registered-models/create"):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"run": {"info": {"artifact_uri": "mlflow-artifacts:/x"}, "data": {"tags": [
            {"key": mlflow.TAG_TENANT, "value": T}, {"key": mlflow.TAG_PROJECT, "value": P}]}}})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return created


def test_registering_a_run_twice_reuses_its_mlflow_version(monkeypatch):
    created = _registry(monkeypatch, [{"version": "4"}])
    assert asyncio.run(mlflow.register_model_version(T, P, "clf", "abc123")) == "4"
    assert created == []


def test_registering_a_new_run_creates_a_version(monkeypatch):
    created = _registry(monkeypatch, [])
    assert asyncio.run(mlflow.register_model_version(T, P, "clf", "abc123")) == "9"
    assert len(created) == 1
