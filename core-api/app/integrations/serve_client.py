"""Ray Serve deployment (req/04 Deployments group, req/07 FR3: Ray Serve).

Shared deployments (deployments.serving = 'shared') have no Serve app of their own. Two long-lived apps, predictor-cpu and
predictor-gpu (the ray_serving package), load each deployment's model by id: `ensure_apps` declares them through Serve's
REST API, `predict` calls one with the deployment's model spec. Declaring replaces every Serve app not in the config, so it
waits while a legacy deployment is still served.

Legacy deployments: deploying is itself a Ray job: the entrypoint calls `serve.run(...)`, which
starts the Serve replica as an independent Ray actor and returns — the
submission job then exits, but the deployment keeps running (this is the
standard way to drive Serve from outside the cluster's own Python process).
core-api then calls the deployed HTTP route directly for /predict.
"""
import ast
import base64
import logging
import textwrap
import time
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from app.core.db import get_pool
from app.core.enums import JobPackageName
from app.core.problem import Problem
from app.core.settings import settings
from app.core.task_catalog import IMAGENET_PREPROCESSING
from app.core.task_types import TaskInfo
from app.integrations import compute, serve_config
from app.integrations.data_plane import job_package, layout, mlflow
from app.integrations.serve_config import SERVE_HTTP_PORT, ComputeTarget

logger = logging.getLogger(__name__)

# Both files live in the ray_serving package and are read as text, never imported here: core-api depends on neither numpy
# nor PIL. A legacy deployment embeds them in its script; the admin task-type routes check function names against them.
INPUTS_FILE = "inputs.py"
POSTPROCESS_FILE = "postprocess.py"


def _source(name: str) -> str:
    return (job_package.SERVING_PACKAGE_DIR / name).read_text()


def _functions(name: str) -> set[str]:
    return {n.name for n in ast.parse(_source(name)).body if isinstance(n, ast.FunctionDef)}


def unknown_serving_functions(encoder: str | None, postprocess: str | None) -> list[str]:
    """The names, of those given, that the serve files do not define."""
    missing = [] if encoder is None or encoder in _functions(INPUTS_FILE) else [encoder]
    return missing + ([] if postprocess is None or postprocess in _functions(POSTPROCESS_FILE) else [postprocess])


def serve_url() -> str:
    """Where legacy routes are served: the configured Ray head (settings.ray.dashboard) at Serve's port, read per call."""
    head = urlsplit(settings.ray.dashboard)
    return f"{head.scheme}://{head.hostname}:{SERVE_HTTP_PORT}"


# req/06 Ultralytics FR3: serves the actual exported ONNX model — real
# onnxruntime inference on real weights, not a stub. Accepts a base64
# image and preprocesses it as model.json says the training job did.
_SERVE_TEMPLATE = """
import json
import urllib.request
from ray import serve
from starlette.requests import Request
import onnxruntime as ort

class_names = {class_names!r}
params = {params!r}
model_url = {model_url!r}
manifest_url = {manifest_url!r}
tokenizer_url = {tokenizer_url!r}

@serve.deployment
class Predictor:
    def __init__(self):
        # Loaded lazily inside the deployment, not at module scope — an
        # onnxruntime InferenceSession can't be pickled, and Ray Serve
        # only pickles the class definition to distribute it, not
        # module-level objects captured in a closure.
        # The model is an artifact of the training run's MLflow run (core-api authorized this deployment).
        onnx_bytes = urllib.request.urlopen(model_url, timeout=120).read()
        self.session = ort.InferenceSession(onnx_bytes, providers={providers!r})
        self.input_name = self.session.get_inputs()[0].name
        # a model trained before the manifest named its resampling filter used the library default
        self.pre = {{"resample": {default_resample!r}, **json.loads(urllib.request.urlopen(manifest_url, timeout=120).read())["preprocessing"]}}
        self.encode = {encoder}(self.pre, tokenizer_url, self.input_name)

    async def __call__(self, request: Request):
        body = await request.json()
        feeds, width, height = self.encode(body)
        outputs = self.session.run(None, feeds)
        return {{"predictions": {postprocess}(outputs, class_names, width, height, params)}}

serve.start(http_options={{"host": "0.0.0.0", "port": {port}}})
serve.run(Predictor.bind(), name="{name}", route_prefix="{route_prefix}")
print("metric:deployed=1")
"""


def route_prefix_for(deployment_id: str, project_id: str, deploy_strategy: str) -> str:
    """The route a deployment is listed under; a shared deployment keeps it as a label (nothing is served there)."""
    return f"/predict/{project_id}/main" if deploy_strategy == "main" else f"/predict/{deployment_id}"


def app_name(deployment_id: str, project_id: str, deploy_strategy: str) -> str:
    """The legacy Serve app of a deployment: one per project under `main`, else one per deployment."""
    return f"deploy-project-{project_id}-main" if deploy_strategy == "main" else f"deploy-{deployment_id}"


async def remove(*, deployment_id: str, project_id: str, tenant_id: str, deploy_strategy: str) -> str:
    """Delete a legacy deployment's Serve app (a Ray job, like deploy: Serve is only driven from inside the cluster).
    Idempotent: serve.delete of an app that is gone does nothing. Returns the job id."""
    name = app_name(deployment_id, project_id, deploy_strategy)
    return await compute.submit(compute.JobSpec(
        entrypoint=f"python -c \"from ray import serve; serve.delete({name!r})\"",
        metadata={"tenant_id": tenant_id, "project_id": project_id, "job_type": "serve-remove"},
    ))


async def deploy(
    *,
    deployment_id: str,
    task: TaskInfo,
    mlflow_run_id: str,
    class_names: list[str],
    tenant_id: str,
    project_id: str,
    compute_target: ComputeTarget = ComputeTarget.CPU,
    deploy_strategy: str = "latest",
) -> tuple[str, str]:
    # FR4/FR5 (06-project-settings-deployment.md): 'main' reuses one stable
    # route per project across deploys (serve.run against the same `name`
    # updates the running replica in place); 'latest' keeps Phase 1's
    # behavior of a permanent per-deployment route.
    route_prefix = route_prefix_for(deployment_id, project_id, deploy_strategy)
    name = app_name(deployment_id, project_id, deploy_strategy)

    model_url = await mlflow.run_model_url(tenant_id, project_id, mlflow_run_id)
    params = task.serving.model_dump(mode="json")
    script = _source(INPUTS_FILE) + _source(POSTPROCESS_FILE) + textwrap.dedent(_SERVE_TEMPLATE).format(
        encoder=task.encoder,
        postprocess=task.postprocess,
        providers=params.pop("providers"),
        params=params,
        tokenizer_url=model_url.removesuffix(layout.MODEL_KEY) + layout.TOKENIZER_KEY,
        model_url=model_url,
        manifest_url=model_url.removesuffix(layout.MODEL_KEY) + layout.MODEL_META_KEY,  # published side by side
        default_resample=IMAGENET_PREPROCESSING.resample.value,
        class_names=class_names,
        name=name,
        route_prefix=route_prefix,
        port=SERVE_HTTP_PORT,
    )

    encoded = base64.b64encode(script.encode()).decode()
    entrypoint = f'python -c "import base64; exec(base64.b64decode(\'{encoded}\'))"'
    job_id = await compute.submit(compute.JobSpec(
        entrypoint=entrypoint,
        metadata={"tenant_id": tenant_id, "project_id": project_id, "job_type": "serve-deploy"},
        num_gpus=1 if compute_target == ComputeTarget.GPU else 0,
    ))
    return job_id, route_prefix


# ---- shared predictor apps ----

SERVE_APPLICATIONS_PATH = "/api/serve/applications/"  # on the dashboard. GET: what runs; PUT: declare the whole set
MODEL_ID_HEADER = "serve_multiplexed_model_id"  # Serve's own name for the id a multiplexed model is picked by
SERVE_API_TIMEOUT_SECONDS = 10


def model_id(deployment_id: str, run_id: str) -> str:
    """What a replica caches a model under. A new run behind the same deployment (a `main` re-deploy) gets a new id."""
    return f"{deployment_id}@{run_id}"


class ServeErrorCode(StrEnum):
    """`error.code` of a failed answer. The first three mirror ray_serving.model.ErrorCode (drift-tested)."""

    MODEL_LOAD_FAILED = "model_load_failed"
    UNKNOWN_FUNCTION = "unknown_function"
    BAD_INPUT = "bad_input"
    UNREACHABLE = "serve_unreachable"
    TIMEOUT = "serve_timeout"
    BAD_ANSWER = "serve_bad_answer"


class ServeCallError(Exception):
    def __init__(self, code: ServeErrorCode, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class ServedModel(BaseModel):
    """What a request carries about its model (ray_serving.model.ModelSpec): where it is and how to run it."""

    model_url: str
    manifest_url: str
    tokenizer_url: str
    class_names: list[str]
    params: dict[str, Any]
    providers: list[str]
    encoder: str
    postprocess: str


class Prediction(BaseModel):
    predictions: Any
    cold_load: bool


async def served_model(task: TaskInfo, mlflow_run_id: str, class_names: list[str], tenant_id: str, project_id: str) -> ServedModel:
    model_url = await mlflow.run_model_url(tenant_id, project_id, mlflow_run_id)
    params = task.serving.model_dump(mode="json")
    return ServedModel(
        model_url=model_url, manifest_url=model_url.removesuffix(layout.MODEL_KEY) + layout.MODEL_META_KEY,
        tokenizer_url=model_url.removesuffix(layout.MODEL_KEY) + layout.TOKENIZER_KEY, class_names=class_names,
        providers=params.pop("providers"), params=params, encoder=task.encoder, postprocess=task.postprocess)


def _dashboard() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=settings.ray.dashboard, timeout=SERVE_API_TIMEOUT_SECONDS)


async def _live_applications(client: httpx.AsyncClient) -> dict:
    resp = await client.get(SERVE_APPLICATIONS_PATH)
    resp.raise_for_status()
    return resp.json().get("applications", {})


async def apps_declared() -> bool:
    """Read-only: both predictor apps are declared on the cluster (by whichever package). The poller is the only writer."""
    try:
        async with _dashboard() as client:
            live = await _live_applications(client)
    except httpx.HTTPError as e:
        logger.warning("Serve applications not readable: %s", e)
        return False
    return all(serve_config.predictor_app(t) in live for t in ComputeTarget)


async def _legacy_active() -> bool:
    async with (await get_pool()).acquire() as conn:
        return bool(await conn.fetchval("SELECT legacy_deployments_active()"))


async def ensure_apps() -> bool:
    """Make the cluster's Serve config the one render() gives for the stable ray_serving package: GET the live config and PUT
    only when it differs (or its zip URL is old). True when it matches afterwards; False when there is no stable package, a
    legacy deployment is still served (declaring would delete its Serve app) or Ray cannot be reached. Idempotent and never
    raises for a down cluster: the poller (the only caller) tries again next tick."""
    try:
        package = await job_package.resolve_stable(JobPackageName.RAY_SERVING)
        async with _dashboard() as client:
            now = time.time()
            ttl = settings.serving.package_url_ttl_seconds
            url = "https://zip.invalid"  # only for the comparison: the real URL is made when a PUT is needed
            if serve_config.up_to_date(await _live_applications(client), serve_config.render(url, package.sha256, int(now)), now, ttl):
                return True
            if await _legacy_active():
                logger.warning("predictor apps not declared: a legacy deployment is still served and declaring would delete its Serve app")
                return False
            url = await job_package.presigned_url(JobPackageName.RAY_SERVING, package.sha256, ttl)
            put = await client.put(SERVE_APPLICATIONS_PATH, json=serve_config.render(url, package.sha256, int(now)))
            put.raise_for_status()
    except (Problem, httpx.HTTPError) as e:
        logger.warning("predictor apps not ensured: %s", e)
        return False
    logger.info("predictor apps declared with ray_serving %s", package.sha256)
    return True


async def ensure_serving() -> None:
    """The poller body (app/background.py, leader-elected): keeps the apps as declared, healing a restarted cluster."""
    await ensure_apps()


def _proxy_url() -> str:
    return settings.serving.url or serve_url()


async def predict(target: ComputeTarget, deployment_id: str, run_id: str, model: ServedModel, body: dict) -> Prediction:
    """Answer `body` with the deployment's model on the shared app, through the Serve Service (a connect error is retried
    `serving.connect_retries` times: another proxy may answer). Raises ServeCallError."""
    cfg = settings.serving
    resp = None
    for attempt in range(cfg.connect_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=cfg.request_timeout_seconds) as client:
                resp = await client.post(
                    f"{_proxy_url()}/{serve_config.predictor_app(target)}", headers={MODEL_ID_HEADER: model_id(deployment_id, run_id)},
                    json={"model": model.model_dump(mode="json"), "input": body})
            break
        except httpx.ConnectError as e:
            if attempt == cfg.connect_retries:
                raise ServeCallError(ServeErrorCode.UNREACHABLE, f"{type(e).__name__}: {e}") from e
        except httpx.TimeoutException as e:
            raise ServeCallError(ServeErrorCode.TIMEOUT, f"no answer within {cfg.request_timeout_seconds}s") from e
        except httpx.HTTPError as e:
            raise ServeCallError(ServeErrorCode.UNREACHABLE, f"{type(e).__name__}: {e}") from e
    try:
        answer = resp.json()
    except ValueError as e:
        raise ServeCallError(ServeErrorCode.BAD_ANSWER, f"serve answered {resp.status_code} with a body that is not JSON") from e
    if isinstance(answer, dict) and "error" in answer:
        try:
            code = ServeErrorCode(answer["error"]["code"])
        except (KeyError, ValueError):
            code = ServeErrorCode.BAD_ANSWER
        raise ServeCallError(code, str(answer["error"].get("message", "")))
    if resp.status_code != httpx.codes.OK:
        raise ServeCallError(ServeErrorCode.BAD_ANSWER, f"serve answered {resp.status_code}")
    return Prediction.model_validate(answer)
