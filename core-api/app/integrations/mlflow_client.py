"""MLflow access for core-api (req/06 FR1/FR2).

MLflow has no per-tenant authorization, so it is internal and core-api is the only door customers
have to it. Isolation is by construction: every experiment and registered model is namespaced by
tenant and project here, and routes reach MLflow ids only through rows the caller can see (RLS plus
the project filter), never from client input.

Runs are created at job submit. The trainer uploads the model straight to MLflow's artifact API
(--serve-artifacts stores it in the bucket); core-api logs metrics and closes the run when the job
ends, so the Ray entrypoint doesn't need mlflow installed.
"""
import time

import httpx

from app.core.settings import settings

MLFLOW_URL = settings.mlflow.url
ARTIFACTS_API = f"{MLFLOW_URL}/api/2.0/mlflow-artifacts/artifacts"
_SCHEME = "mlflow-artifacts:/"


def experiment_name(tenant_id: str, project_id: str) -> str:
    return f"tenants/{tenant_id}/projects/{project_id}"


def registered_model_name(tenant_id: str, project_id: str, name: str) -> str:
    # Registered model names are global in MLflow: without the prefix two tenants naming a model
    # "classifier" would share versions and stages.
    return f"{tenant_id}.{project_id}.{name}"


def display_name(full_name: str, tenant_id: str, project_id: str) -> str:
    return full_name.removeprefix(f"{tenant_id}.{project_id}.")


async def _get_or_create_experiment(client: httpx.AsyncClient, name: str) -> str:
    params = {"experiment_name": name}
    resp = await client.get(f"{MLFLOW_URL}/api/2.0/mlflow/experiments/get-by-name", params=params)
    if resp.status_code == 200:
        return resp.json()["experiment"]["experiment_id"]
    resp = await client.post(f"{MLFLOW_URL}/api/2.0/mlflow/experiments/create", json={"name": name})
    if resp.status_code == 400:  # lost a create race with another request
        resp = await client.get(f"{MLFLOW_URL}/api/2.0/mlflow/experiments/get-by-name", params=params)
        resp.raise_for_status()
        return resp.json()["experiment"]["experiment_id"]
    resp.raise_for_status()
    return resp.json()["experiment_id"]


async def start_run(*, tenant_id: str, project_id: str, dataset_ref: str) -> tuple[str, str]:
    """Create the run before the job is submitted. Returns (run_id, artifact_path), where
    artifact_path is the run's artifact root relative to the artifact API (`<exp>/<run>/artifacts`)."""
    async with httpx.AsyncClient(timeout=10) as client:
        experiment_id = await _get_or_create_experiment(client, experiment_name(tenant_id, project_id))
        resp = await client.post(
            f"{MLFLOW_URL}/api/2.0/mlflow/runs/create",
            json={
                "experiment_id": experiment_id,
                "start_time": int(time.time() * 1000),
                "tags": [
                    {"key": "tenant_id", "value": tenant_id},
                    {"key": "project_id", "value": project_id},
                    {"key": "dataset_commit", "value": dataset_ref},
                ],
            },
        )
        resp.raise_for_status()
    info = resp.json()["run"]["info"]
    if not info["artifact_uri"].startswith(_SCHEME):
        raise RuntimeError(f"MLflow is not serving artifacts (artifact_uri={info['artifact_uri']})")
    return info["run_id"], info["artifact_uri"].removeprefix(_SCHEME).strip("/")


async def finish_run(*, run_id: str, status: str, metrics: dict[str, float] | None = None) -> None:
    """status: FINISHED | FAILED | KILLED."""
    now_ms = int(time.time() * 1000)
    async with httpx.AsyncClient(timeout=10) as client:
        if metrics:
            resp = await client.post(
                f"{MLFLOW_URL}/api/2.0/mlflow/runs/log-batch",
                json={
                    "run_id": run_id,
                    "metrics": [
                        {"key": k, "value": v, "timestamp": now_ms, "step": 0} for k, v in metrics.items()
                    ],
                },
            )
            resp.raise_for_status()
        resp = await client.post(
            f"{MLFLOW_URL}/api/2.0/mlflow/runs/update",
            json={"run_id": run_id, "status": status, "end_time": now_ms},
        )
        resp.raise_for_status()


async def register_model_version(*, name: str, run_id: str) -> str:
    """req/06 FR2: Model Registry stages map to customer-facing states
    (None -> Staging -> Production). `name` is the already-namespaced registered model name."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{MLFLOW_URL}/api/2.0/mlflow/registered-models/create", json={"name": name})
        if resp.status_code not in (200, 400):  # 400 = RESOURCE_ALREADY_EXISTS, fine
            resp.raise_for_status()

        resp = await client.post(
            f"{MLFLOW_URL}/api/2.0/mlflow/model-versions/create",
            json={"name": name, "source": f"runs:/{run_id}/model", "run_id": run_id},
        )
        resp.raise_for_status()
        return resp.json()["model_version"]["version"]


async def transition_stage(*, name: str, version: str, stage: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{MLFLOW_URL}/api/2.0/mlflow/model-versions/transition-stage",
            json={"name": name, "version": version, "stage": stage, "archive_existing_versions": False},
        )
        resp.raise_for_status()
        return resp.json()["model_version"]
