"""MLflow tracking REST API client (req/06-ml-training-mlflow-ludwig.md FR1).

Called from core-api after a training job completes (see app/modules/training/routes.py) —
not from inside the Ray job — so the Ray training entrypoint doesn't need
mlflow installed on the cluster.
"""
import time

import httpx

from app.core import config

MLFLOW_URL = config.MLFLOW_URL


async def _get_or_create_experiment(client: httpx.AsyncClient, name: str) -> str:
    resp = await client.get(
        f"{MLFLOW_URL}/api/2.0/mlflow/experiments/get-by-name", params={"experiment_name": name}
    )
    if resp.status_code == 200:
        return resp.json()["experiment"]["experiment_id"]
    resp = await client.post(
        f"{MLFLOW_URL}/api/2.0/mlflow/experiments/create", json={"name": name}
    )
    resp.raise_for_status()
    return resp.json()["experiment_id"]


async def log_training_run(
    *,
    tenant_id: str,
    project_id: str,
    dataset_ref: str,
    metrics: dict[str, float],
    succeeded: bool,
) -> str:
    """FR1: log params/metrics tagged tenant_id/project_id/dataset_commit.
    FR3: registry lineage back to the dataset version it trained on."""
    async with httpx.AsyncClient(timeout=10) as client:
        experiment_id = await _get_or_create_experiment(client, f"tenant-{tenant_id}")

        now_ms = int(time.time() * 1000)
        resp = await client.post(
            f"{MLFLOW_URL}/api/2.0/mlflow/runs/create",
            json={
                "experiment_id": experiment_id,
                "start_time": now_ms,
                "tags": [
                    {"key": "tenant_id", "value": tenant_id},
                    {"key": "project_id", "value": project_id},
                    {"key": "dataset_commit", "value": dataset_ref},
                ],
            },
        )
        resp.raise_for_status()
        run_id = resp.json()["run"]["info"]["run_id"]

        for key, value in metrics.items():
            resp = await client.post(
                f"{MLFLOW_URL}/api/2.0/mlflow/runs/log-metric",
                json={"run_id": run_id, "key": key, "value": value, "timestamp": now_ms},
            )
            resp.raise_for_status()

        await client.post(
            f"{MLFLOW_URL}/api/2.0/mlflow/runs/update",
            json={
                "run_id": run_id,
                "status": "FINISHED" if succeeded else "FAILED",
                "end_time": int(time.time() * 1000),
            },
        )
    return run_id


async def register_model_version(*, name: str, run_id: str) -> str:
    """req/06 FR2: Model Registry stages map to customer-facing states
    (None -> Staging -> Production). Real MLflow Model Registry calls, not
    a local table pretending to be one."""
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


async def get_model_version(*, name: str, version: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{MLFLOW_URL}/api/2.0/mlflow/model-versions/get", params={"name": name, "version": version}
        )
        resp.raise_for_status()
        return resp.json()["model_version"]


async def search_model_versions(*, name: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{MLFLOW_URL}/api/2.0/mlflow/model-versions/search", params={"filter": f"name='{name}'"}
        )
        resp.raise_for_status()
        return resp.json().get("model_versions", [])
