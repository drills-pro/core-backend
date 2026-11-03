"""The shared predictor apps: the config core-api declares, when it declares it, and the call that answers a request."""
import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.core.enums import JobPackageName, PackageStatus
from app.core.problem import Problem
from app.integrations import serve_client
from app.integrations.serve_client import ComputeTarget, ServedModel, ServeErrorCode

SHA = "e" * 64
NOW = 1_000_000.0
RAY_SERVING_MODEL = Path(__file__).parents[3] / "ray-serving" / "ray_serving" / "model.py"
MODEL = ServedModel(
    model_url="http://m/model.onnx", manifest_url="http://m/model.json", tokenizer_url="http://m/tokenizer.json", class_names=["a", "b"],
    params={"score_threshold": 0.3}, providers=["CPUExecutionProvider"], encoder="image_encoder", postprocess="classify")


def declared(sha=SHA, issued=NOW):
    app = {"deployed_app_config": {"runtime_env": {"env_vars": {serve_client.PACKAGE_SHA_ENV: sha, serve_client.URL_ISSUED_ENV: str(int(issued))}}}}
    return {"applications": {serve_client.predictor_app(t): app for t in ComputeTarget}}


def test_both_apps_are_declared_with_the_package_and_a_zip_url():
    cfg = serve_client.serve_config("http://zip", SHA, 5)
    apps = {a["name"]: a for a in cfg["applications"]}
    assert set(apps) == {"predictor-cpu", "predictor-gpu"}
    cpu, gpu = apps["predictor-cpu"], apps["predictor-gpu"]
    assert cpu["import_path"] == "ray_serving.app:build" and cpu["runtime_env"]["py_modules"] == ["http://zip"]
    assert cpu["runtime_env"]["env_vars"] == {serve_client.PACKAGE_SHA_ENV: SHA, serve_client.URL_ISSUED_ENV: "5"}
    assert cpu["deployments"][0]["ray_actor_options"]["num_gpus"] == 0 and gpu["deployments"][0]["ray_actor_options"]["num_gpus"] == 1
    assert cpu["args"] == {"max_models_per_replica": serve_client.settings.serving.max_models_per_replica}
    assert cfg["http_options"]["port"] == serve_client.SERVE_HTTP_PORT


def test_declared_means_this_package_and_a_zip_url_with_most_of_its_life_left():
    ttl = serve_client.settings.serving.package_url_ttl_seconds
    assert serve_client._declared(declared()["applications"], SHA, NOW)
    assert not serve_client._declared(declared()["applications"], SHA, NOW + ttl)  # past half its life: re-declare with a fresh URL
    assert not serve_client._declared(declared(sha="f" * 64)["applications"], SHA, NOW)
    assert not serve_client._declared({}, SHA, NOW)


@pytest.fixture
def serve(monkeypatch):
    """Serve's REST API as a list of requests and a canned GET; the package registry and legacy count are stubs."""
    state = SimpleNamespace(applications={}, legacy=False, requests=[], package=True)

    def handler(request: httpx.Request) -> httpx.Response:
        state.requests.append((request.method, request.url.path, request.content))
        if request.method == "GET":
            return httpx.Response(200, json={"applications": state.applications})
        return httpx.Response(200)

    real = state.real = httpx.AsyncClient
    monkeypatch.setattr(serve_client.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    async def resolve_stable(name):
        if not state.package:
            raise Problem(503, "no_stable_package", "none")
        return SimpleNamespace(sha256=SHA, status=PackageStatus.STABLE)

    async def presigned_url(name, sha, ttl=None):
        assert name == JobPackageName.RAY_SERVING
        return "http://zip"

    async def legacy_active():
        return state.legacy

    monkeypatch.setattr(serve_client.job_package, "resolve_stable", resolve_stable)
    monkeypatch.setattr(serve_client.job_package, "presigned_url", presigned_url)
    monkeypatch.setattr(serve_client, "_legacy_active", legacy_active)
    monkeypatch.setattr(serve_client.time, "time", lambda: NOW)
    return state


def puts(state):
    return [r for r in state.requests if r[0] == "PUT"]


def test_the_apps_are_declared_when_they_are_not_there(serve):
    assert asyncio.run(serve_client.ensure_apps()) is True
    (_, path, body), = puts(serve)
    assert path == serve_client.SERVE_APPLICATIONS_PATH and json.loads(body)["applications"][0]["runtime_env"]["py_modules"] == ["http://zip"]


def test_declared_apps_are_left_alone(serve):
    serve.applications = declared()["applications"]
    assert asyncio.run(serve_client.ensure_apps()) is True and not puts(serve)


def test_a_legacy_deployment_still_served_keeps_the_apps_undeclared(serve):
    serve.legacy = True
    assert asyncio.run(serve_client.ensure_apps()) is False and not puts(serve)


def test_no_stable_package_or_no_serve_is_false_not_an_error(serve, monkeypatch):
    serve.package = False
    assert asyncio.run(serve_client.ensure_apps()) is False
    serve.package = True
    real = serve.real
    monkeypatch.setattr(serve_client.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(503)), **kw))
    assert asyncio.run(serve_client.ensure_apps()) is False


@pytest.fixture
def answer(monkeypatch):
    sent = []

    def respond(reply):
        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            if isinstance(reply, Exception):
                raise reply
            return reply

        real = httpx.AsyncClient
        monkeypatch.setattr(serve_client.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
        return sent

    return respond


def call():
    return asyncio.run(serve_client.predict(ComputeTarget.GPU, "dep", "run", MODEL, {"text": "x"}))


def test_a_prediction_goes_to_the_targets_app_under_the_deployment_and_run_id(answer):
    sent = answer(httpx.Response(200, json={"predictions": [1], "cold_load": True}))
    result = call()
    request = sent[0]
    assert request.url.path == "/predictor-gpu" and request.headers[serve_client.MODEL_ID_HEADER] == "dep@run"
    assert json.loads(request.content) == {"model": MODEL.model_dump(mode="json"), "input": {"text": "x"}}
    assert (result.predictions, result.cold_load) == ([1], True)


@pytest.mark.parametrize("reply,code", [
    (httpx.Response(422, json={"error": {"code": "bad_input", "message": "m"}}), ServeErrorCode.BAD_INPUT),
    (httpx.Response(500, json={"error": {"code": "model_load_failed", "message": "m"}}), ServeErrorCode.MODEL_LOAD_FAILED),
    (httpx.Response(500, json={"error": {"code": "something_new"}}), ServeErrorCode.BAD_ANSWER),
    (httpx.Response(502, text="<html>"), ServeErrorCode.BAD_ANSWER),
    (httpx.Response(500, json={}), ServeErrorCode.BAD_ANSWER),
    (httpx.ReadTimeout("slow"), ServeErrorCode.TIMEOUT),
    (httpx.ConnectError("down"), ServeErrorCode.UNREACHABLE)])
def test_a_failed_call_is_a_typed_error(answer, reply, code):
    answer(reply)
    with pytest.raises(serve_client.ServeCallError) as err:
        call()
    assert err.value.code == code


def _class_body(name: str) -> ast.ClassDef:
    return next(n for n in ast.parse(RAY_SERVING_MODEL.read_text()).body if isinstance(n, ast.ClassDef) and n.name == name)


def test_the_replicas_error_codes_and_model_spec_are_the_ones_core_api_reads_and_sends():
    codes = {n.value.value for n in _class_body("ErrorCode").body if isinstance(n, ast.Assign)}
    assert codes <= {c.value for c in ServeErrorCode}
    fields = [n.target.id for n in _class_body("ModelSpec").body if isinstance(n, ast.AnnAssign)]
    assert fields == list(ServedModel.model_fields)
