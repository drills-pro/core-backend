"""MLflow operations, tenant-scoped by construction (req/06 FR1/FR2).

MLflow has no authorization of its own, so this package is the only door to it. Experiments and
registered models are namespaced by tenant and project here, and every operation checks that the project
(and, for an existing run, the run's tenant tag) belongs to the caller before it calls MLflow.

Runs are created at job submit. The trainer uploads the model straight to MLflow's artifact API; core-api
logs metrics and closes the run when the job ends.
"""
import asyncio
import time
from enum import StrEnum

import httpx
from botocore.exceptions import ClientError
from pydantic import BaseModel

from app.core import secret_files
from app.core.problem import not_found
from app.core.settings import settings
from app.integrations.data_plane import access, lakefs, layout

_TIMEOUT = 10
_HEALTH_TIMEOUT = 3
_S3_NO_SUCH_KEY = "NoSuchKey"
_UPLOAD_TIMEOUT = httpx.Timeout(_TIMEOUT, write=600, read=600)  # a model file is hundreds of MB
_UPLOAD_CHUNK = 1 << 20
MODEL_DIR = "model"  # artifact directory a registered version's source `runs:/<run>/model` points at
_SCHEME = "mlflow-artifacts:/"
_HTTP_BAD_REQUEST = 400
TAG_TENANT, TAG_PROJECT, TAG_DATASET_VERSION = "tenant_id", "project_id", "dataset_version_id"
TAG_TRAINING_RUN = "training_run_id"  # on a model version: the training run it was made from


class RunStatus(StrEnum):
    FINISHED = "FINISHED"
    FAILED = "FAILED"
    KILLED = "KILLED"


class ModelStage(StrEnum):
    """Model Registry stages."""

    NONE = "None"
    STAGING = "Staging"
    PRODUCTION = "Production"
    ARCHIVED = "Archived"


class StartedRun(BaseModel):
    run_id: str
    artifact_path: str  # the run's artifact root relative to the artifact API: `<exp>/<run>/artifacts`


def experiment_name(tenant_id: str, project_id: str) -> str:
    return f"tenants/{tenant_id}/projects/{project_id}"


def registered_model_name(tenant_id: str, project_id: str, name: str) -> str:
    # Registered model names are global in MLflow: without the prefix two tenants naming a model
    # "classifier" would share versions and stages.
    return f"{tenant_id}.{project_id}.{name}"


def display_name(full_name: str, tenant_id: str, project_id: str) -> str:
    return full_name.removeprefix(f"{tenant_id}.{project_id}.")


def _api(path: str) -> str:
    return f"{settings.mlflow.url}/api/2.0/mlflow/{path}"


def artifact_url(path: str) -> str:
    """URL a trainer uploads to, or serving downloads from, for a path relative to MLflow's artifact API."""
    return f"{settings.mlflow.url}/api/2.0/mlflow-artifacts/artifacts/{path}"


async def health() -> str:
    try:
        async with httpx.AsyncClient(timeout=_HEALTH_TIMEOUT) as client:
            (await client.get(f"{settings.mlflow.url}/health")).raise_for_status()
        return layout.HEALTH_OK
    except Exception as e:  # noqa: BLE001 - health check, report any failure
        return f"{layout.HEALTH_ERROR}{e}"


async def _get_or_create_experiment(client: httpx.AsyncClient, name: str) -> str:
    params = {"experiment_name": name}
    resp = await client.get(_api("experiments/get-by-name"), params=params)
    if resp.status_code == httpx.codes.OK:
        return resp.json()["experiment"]["experiment_id"]
    resp = await client.post(_api("experiments/create"), json={"name": name})
    if resp.status_code == _HTTP_BAD_REQUEST:  # lost a create race with another request
        resp = await client.get(_api("experiments/get-by-name"), params=params)
        resp.raise_for_status()
        return resp.json()["experiment"]["experiment_id"]
    resp.raise_for_status()
    return resp.json()["experiment_id"]


async def _require_run(client: httpx.AsyncClient, tenant_id: str, project_id: str, run_id: str) -> dict:
    """The run (MLflow's `run` object) if it carries this tenant's and project's tags; anything else is a 404."""
    resp = await client.get(_api("runs/get"), params={"run_id": run_id})
    if resp.status_code != httpx.codes.OK:
        raise not_found()
    tags = {t["key"]: t["value"] for t in resp.json()["run"]["data"].get("tags", [])}
    if tags.get(TAG_TENANT) != tenant_id or tags.get(TAG_PROJECT) != project_id:
        raise not_found()
    return resp.json()["run"]


async def start_run(tenant_id: str, project_id: str, dataset_id: str, dataset_version_id: str) -> StartedRun:
    """Create the run before the job is submitted."""
    await access.project(tenant_id, project_id)
    await access.version_commit(tenant_id, dataset_id, dataset_version_id)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        experiment_id = await _get_or_create_experiment(client, experiment_name(tenant_id, project_id))
        resp = await client.post(
            _api("runs/create"),
            json={
                "experiment_id": experiment_id,
                "start_time": int(time.time() * 1000),
                "tags": [
                    {"key": TAG_TENANT, "value": tenant_id},
                    {"key": TAG_PROJECT, "value": project_id},
                    {"key": TAG_DATASET_VERSION, "value": dataset_version_id},
                ],
            },
        )
        resp.raise_for_status()
    info = resp.json()["run"]["info"]
    if not info["artifact_uri"].startswith(_SCHEME):
        raise RuntimeError(f"MLflow is not serving artifacts (artifact_uri={info['artifact_uri']})")
    return StartedRun(run_id=info["run_id"], artifact_path=info["artifact_uri"].removeprefix(_SCHEME).strip("/"))


async def finish_run(
    tenant_id: str, project_id: str, run_id: str, status: RunStatus, metrics: dict[str, float] | None = None
) -> None:
    await access.project(tenant_id, project_id)
    now_ms = int(time.time() * 1000)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        await _require_run(client, tenant_id, project_id, run_id)
        if metrics:
            batch = [{"key": k, "value": v, "timestamp": now_ms, "step": 0} for k, v in metrics.items()]
            resp = await client.post(_api("runs/log-batch"), json={"run_id": run_id, "metrics": batch})
            resp.raise_for_status()
        resp = await client.post(_api("runs/update"), json={"run_id": run_id, "status": status, "end_time": now_ms})
        resp.raise_for_status()


async def _chunks(body):
    while chunk := await asyncio.to_thread(body.read, _UPLOAD_CHUNK):
        yield chunk


async def publish_run_model(tenant_id: str, project_id: str, run_id: str, mlflow_run_id: str) -> None:
    """Copy the trainer's model.onnx and model.json from the run's staging prefix into the MLflow run's `model/`
    artifacts, so a registered version has something to point at. The trainer holds no MLflow credential (plan T7)."""
    await access.training_run(tenant_id, project_id, run_id)
    prefix = layout.train_prefix(tenant_id, project_id, run_id)
    async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT) as client:
        run = await _require_run(client, tenant_id, project_id, mlflow_run_id)
        s3 = lakefs.s3_client(secret_files.STAGING)
        root = run["info"]["artifact_uri"].removeprefix(_SCHEME).strip("/")
        for name in (layout.MODEL_KEY, layout.MODEL_META_KEY, layout.TOKENIZER_KEY):
            try:
                obj = await asyncio.to_thread(s3.get_object, Bucket=settings.data_plane.staging_bucket, Key=prefix + name)
            except ClientError as e:
                if name == layout.TOKENIZER_KEY and e.response["Error"]["Code"] == _S3_NO_SUCH_KEY:
                    continue  # only a text model has one
                raise
            resp = await client.put(
                artifact_url(f"{root}/{MODEL_DIR}/{name}"), content=_chunks(obj["Body"]),
                headers={"Content-Length": str(obj["ContentLength"])},
            )
            resp.raise_for_status()


async def run_model_url(tenant_id: str, project_id: str, mlflow_run_id: str) -> str:
    """Where serving downloads a run's published `model.onnx`: under the run's artifact root, through MLflow's artifact API."""
    await access.project(tenant_id, project_id)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        run = await _require_run(client, tenant_id, project_id, mlflow_run_id)
    root = run["info"]["artifact_uri"].removeprefix(_SCHEME).strip("/")
    return artifact_url(f"{root}/{MODEL_DIR}/{layout.MODEL_KEY}")


async def open_model_file(tenant_id: str, project_id: str, mlflow_run_id: str, name: str) -> tuple[httpx.AsyncClient, httpx.Response]:
    """A run's published model file as an open streamed response; the caller closes both. 404 when the run is not this project's
    or the file is absent. Streaming keeps a multi-hundred-MB model out of memory."""
    await access.project(tenant_id, project_id)
    client = httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT)
    try:
        run = await _require_run(client, tenant_id, project_id, mlflow_run_id)
        root = run["info"]["artifact_uri"].removeprefix(_SCHEME).strip("/")
        resp = await client.send(client.build_request("GET", artifact_url(f"{root}/{MODEL_DIR}/{name}")), stream=True)
        if resp.status_code != httpx.codes.OK:
            await resp.aclose()
            raise not_found()
    except BaseException:
        await client.aclose()
        raise
    return client, resp


async def register_model_version(tenant_id: str, project_id: str, model_name: str, run_id: str, training_run_id: str) -> str:
    """One version of the project's model for this MLflow run, tagged with its training run. Idempotent per run."""
    await access.project(tenant_id, project_id)
    name = registered_model_name(tenant_id, project_id, model_name)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        await _require_run(client, tenant_id, project_id, run_id)
        resp = await client.post(_api("registered-models/create"), json={"name": name})
        if resp.status_code not in (httpx.codes.OK, _HTTP_BAD_REQUEST):  # 400 = already exists, fine
            resp.raise_for_status()
        existing = await _version_of_run(client, name, run_id)  # a retry after a rolled-back settle finds its own version
        if existing is not None:
            return existing
        resp = await client.post(
            _api("model-versions/create"),
            json={
                "name": name,
                "source": f"runs:/{run_id}/model",
                "run_id": run_id,
                "tags": [{"key": TAG_TRAINING_RUN, "value": training_run_id}],
            },
        )
        resp.raise_for_status()
        return resp.json()["model_version"]["version"]


async def _version_of_run(client: httpx.AsyncClient, name: str, run_id: str) -> str | None:
    """The version already created from this MLflow run, if any; makes registering a run idempotent."""
    resp = await client.get(_api("model-versions/search"), params={"filter": f"name='{name}' and run_id='{run_id}'"})
    resp.raise_for_status()
    versions = resp.json().get("model_versions", [])
    return versions[0]["version"] if versions else None
