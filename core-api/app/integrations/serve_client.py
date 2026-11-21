"""Ray Serve (req/04 Deployments group, req/07 FR3: Ray Serve).

A deployment has no Serve app of its own. Two long-lived apps, predictor-cpu and predictor-gpu (the ray_serving package),
load each deployment's model by id: `ensure_apps` declares them through Serve's REST API, `predict` calls one with the
deployment's model spec. Declaring replaces every Serve app not in the config.
"""
import logging
import time
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from app.core.enums import JobPackageName
from app.core.problem import Problem
from app.core.settings import settings
from app.core.task_types import TaskInfo
from app.integrations import data_plane, serve_config
from app.integrations.data_plane import MODEL_KEY, MODEL_META_KEY, TOKENIZER_KEY
from app.integrations.serve_config import SERVE_HTTP_PORT, ComputeTarget

logger = logging.getLogger(__name__)

def serve_url() -> str:
    """The Serve proxy on the configured Ray head (settings.ray.dashboard) at Serve's port, read per call."""
    head = urlsplit(settings.ray.dashboard)
    return f"{head.scheme}://{head.hostname}:{SERVE_HTTP_PORT}"


def route_prefix_for(deployment_id: str, project_id: str, deploy_strategy: str) -> str:
    """The route a deployment is listed under; a shared deployment keeps it as a label (nothing is served there)."""
    return f"/predict/{project_id}/main" if deploy_strategy == "main" else f"/predict/{deployment_id}"


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
    model_url = await data_plane.run_model_url(tenant_id, project_id, mlflow_run_id)
    params = task.serving.model_dump(mode="json")
    return ServedModel(
        model_url=model_url, manifest_url=model_url.removesuffix(MODEL_KEY) + MODEL_META_KEY,
        tokenizer_url=model_url.removesuffix(MODEL_KEY) + TOKENIZER_KEY, class_names=class_names,
        providers=params.pop("providers"), params=params, encoder=task.encoder, postprocess=task.postprocess)


def _dashboard() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=settings.ray.dashboard, timeout=SERVE_API_TIMEOUT_SECONDS)


async def _live_applications(client: httpx.AsyncClient) -> dict:
    resp = await client.get(SERVE_APPLICATIONS_PATH)
    resp.raise_for_status()
    return resp.json().get("applications", {})


async def ensure_apps() -> bool:
    """Make the cluster's Serve config the one render() gives for the stable ray_serving package: GET the live config and PUT
    only when it differs (or its zip URL is old). True when it matches afterwards; False when there is no stable package or
    Ray cannot be reached. Idempotent and never raises for a down cluster: the poller (the only caller) tries again next tick."""
    try:
        package = await data_plane.resolve_stable_package(JobPackageName.RAY_SERVING)
        async with _dashboard() as client:
            now = time.time()
            ttl = settings.serving.package_url_ttl_seconds
            url = "https://zip.invalid"  # only for the comparison: the real URL is made when a PUT is needed
            if serve_config.up_to_date(await _live_applications(client), serve_config.render(url, package.sha256, int(now), requirements=package.requirements), now, ttl):
                return True
            url = await data_plane.presigned_package_url(JobPackageName.RAY_SERVING, package.sha256, ttl)
            put = await client.put(SERVE_APPLICATIONS_PATH, json=serve_config.render(url, package.sha256, int(now), requirements=package.requirements))
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
