"""MLflow operations, tenant-scoped by construction (req/06 FR1/FR2).

MLflow has no authorization of its own, so this package is the only door to it. Experiments and
registered models are namespaced by tenant and project here, and every operation checks that the project
(and, for an existing run, the run's tenant tag) belongs to the caller before it calls MLflow.

Runs are created at job submit. The trainer uploads the model straight to MLflow's artifact API; core-api
logs metrics and closes the run when the job ends.
"""
import time
from enum import StrEnum

import httpx
from pydantic import BaseModel

from app.core.problem import not_found
from app.core.settings import settings
from app.integrations.data_plane import access, layout

_TIMEOUT = 10
_HEALTH_TIMEOUT = 3
_SCHEME = "mlflow-artifacts:/"
_HTTP_BAD_REQUEST = 400
TAG_TENANT, TAG_PROJECT, TAG_DATASET_VERSION = "tenant_id", "project_id", "dataset_version_id"


class RunStatus(StrEnum):
    FINISHED = "FINISHED"
    FAILED = "FAILED"
    KILLED = "KILLED"


class ModelStage(StrEnum):
    """Model Registry stages."""

    NONE = "None"
    STAGING = "Staging"
    PRODUCTION = "Production"


class StartedRun(BaseModel):
    run_id: str
    artifact_path: str  # the run's artifact root relative to the artifact API: `<exp>/<run>/artifacts`


class ModelVersion(BaseModel):
    name: str
    version: str
    current_stage: str


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


async def _require_run(client: httpx.AsyncClient, tenant_id: str, project_id: str, run_id: str) -> None:
    """The run must carry this tenant's and project's tags; anything else is a 404."""
    resp = await client.get(_api("runs/get"), params={"run_id": run_id})
    if resp.status_code != httpx.codes.OK:
        raise not_found()
    tags = {t["key"]: t["value"] for t in resp.json()["run"]["data"].get("tags", [])}
    if tags.get(TAG_TENANT) != tenant_id or tags.get(TAG_PROJECT) != project_id:
        raise not_found()


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


async def register_model_version(tenant_id: str, project_id: str, model_name: str, run_id: str) -> str:
    """Model Registry stages map to customer-facing states (None -> Staging -> Production)."""
    await access.project(tenant_id, project_id)
    name = registered_model_name(tenant_id, project_id, model_name)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        await _require_run(client, tenant_id, project_id, run_id)
        resp = await client.post(_api("registered-models/create"), json={"name": name})
        if resp.status_code not in (httpx.codes.OK, _HTTP_BAD_REQUEST):  # 400 = already exists, fine
            resp.raise_for_status()
        resp = await client.post(
            _api("model-versions/create"), json={"name": name, "source": f"runs:/{run_id}/model", "run_id": run_id}
        )
        resp.raise_for_status()
        return resp.json()["model_version"]["version"]


async def transition_stage(tenant_id: str, project_id: str, model_name: str, version: str, stage: ModelStage) -> ModelVersion:
    await access.project(tenant_id, project_id)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            _api("model-versions/transition-stage"),
            json={
                "name": registered_model_name(tenant_id, project_id, model_name),
                "version": version,
                "stage": stage,
                "archive_existing_versions": False,
            },
        )
        resp.raise_for_status()
        return ModelVersion.model_validate(resp.json()["model_version"])
