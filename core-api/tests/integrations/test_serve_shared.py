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
from app.integrations import serve_client, serve_config
from app.integrations.serve_client import ServedModel, ServeErrorCode
from app.integrations.serve_config import ComputeTarget

SHA = "e" * 64
NOW = 1_000_000.0
RAY_SERVING_MODEL = Path(__file__).parents[3] / "ray-serving" / "ray_serving" / "model.py"
MODEL = ServedModel(
    model_url="http://m/model.onnx", manifest_url="http://m/model.json", tokenizer_url="http://m/tokenizer.json", class_names=["a", "b"],
    params={"score_threshold": 0.3}, providers=["CPUExecutionProvider"], encoder="image_encoder", postprocess="classify")


def live(sha=SHA, issued=NOW, mutate=None):
    """What GET /api/serve/applications/ reports after the apps were declared with `sha`."""
    apps = serve_config.render("http://zip", sha, int(issued))["applications"]
    if mutate:
        mutate(apps)
    return {a["name"]: {"deployed_app_config": a} for a in apps}


def wanted(sha=SHA):
    return serve_config.render("http://other-zip", sha, 0)  # the URL and its age are not compared


def test_both_apps_are_rendered_with_the_package_the_pool_pin_and_the_failure_settings():
    cfg = serve_config.render("http://zip", SHA, 5)
    apps = {a["name"]: a for a in cfg["applications"]}
    assert set(apps) == {"predictor-cpu", "predictor-gpu"}
    cpu, gpu = apps["predictor-cpu"], apps["predictor-gpu"]
    assert cpu["import_path"] == "ray_serving.app:build" and cpu["runtime_env"]["py_modules"] == ["http://zip"]
    assert cpu["runtime_env"]["env_vars"] == {serve_config.PACKAGE_SHA_ENV: SHA, serve_config.URL_ISSUED_ENV: "5"}
    cpu_dep, gpu_dep = cpu["deployments"][0], gpu["deployments"][0]
    assert cpu_dep["ray_actor_options"] == {"num_cpus": 1, "num_gpus": 0, "resources": {"cpu_pool": 0.01}}  # on the CPU pool
    assert gpu_dep["ray_actor_options"] == {"num_cpus": 1, "num_gpus": 1}
    assert cpu_dep["autoscaling_config"]["min_replicas"] >= 2  # one replica may fail without taking the app down
    assert {"health_check_period_s", "health_check_timeout_s", "graceful_shutdown_timeout_s", "max_ongoing_requests"} <= set(cpu_dep)
    assert cpu["args"] == {"max_models_per_replica": serve_client.settings.serving.max_models_per_replica}
    assert cfg["proxy_location"] == "EveryNode" and cfg["http_options"]["port"] == serve_config.SERVE_HTTP_PORT


def test_the_live_config_is_up_to_date_when_it_holds_the_rendered_one_and_its_zip_url_is_young():
    ttl = serve_client.settings.serving.package_url_ttl_seconds
    assert serve_config.up_to_date(live(), wanted(), NOW, ttl)
    assert not serve_config.up_to_date(live(), wanted(), NOW + ttl, ttl)  # past half its life: re-declare with a fresh URL
    assert not serve_config.up_to_date(live(sha="f" * 64), wanted(), NOW, ttl)
    assert not serve_config.up_to_date({}, wanted(), NOW, ttl)


def test_a_changed_setting_makes_the_live_config_out_of_date_but_defaults_serve_adds_do_not():
    ttl = serve_client.settings.serving.package_url_ttl_seconds
    assert not serve_config.up_to_date(live(mutate=lambda apps: apps[0]["deployments"][0]["autoscaling_config"].update(min_replicas=1)), wanted(), NOW, ttl)
    assert serve_config.up_to_date(live(mutate=lambda apps: apps[0]["deployments"][0].update(user_config=None, version="v")), wanted(), NOW, ttl)


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
    assert json.loads(body)["proxy_location"] == "EveryNode"


def test_a_changed_setting_is_declared_again_and_a_down_ray_is_tried_next_tick(serve, monkeypatch):
    serve.applications = live(mutate=lambda apps: apps[0]["deployments"][0].update(max_ongoing_requests=1))
    assert asyncio.run(serve_client.ensure_apps()) is True and len(puts(serve)) == 1


def test_declared_apps_are_left_alone(serve):
    serve.applications = live()
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


def test_a_connect_error_is_retried_and_a_service_url_replaces_the_heads_address(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if len(seen) < 3:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"predictions": [], "cold_load": False})

    real = httpx.AsyncClient
    monkeypatch.setattr(serve_client.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(serve_client.settings.serving, "url", "http://serve-svc:8000")
    call()
    assert seen == ["http://serve-svc:8000/predictor-gpu"] * 3  # connect_retries = 2


def test_a_timeout_is_not_retried(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        raise httpx.ReadTimeout("slow")

    real = httpx.AsyncClient
    monkeypatch.setattr(serve_client.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(serve_client.ServeCallError):
        call()
    assert len(seen) == 1


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
