import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query

import logging

from app import lakefs_client, metrics as app_metrics
from app import mlflow_client, ray_client, usage
from app.db import get_pool
from app.tenancy import get_tenant_id
from app.tenant_conn import TenantConn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects/{project_id}/training-runs", tags=["training"])

MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_TRAINING_RUNS", "3"))
MIN_LABELED_SAMPLES = 10  # FR3 (03-dataset-governance.md)

_RAY_TO_STATUS = {
    "PENDING": "submitted",
    "RUNNING": "running",
    "SUCCEEDED": "succeeded",
    "FAILED": "failed",
    "STOPPED": "cancelled",
}


@router.post("", status_code=202)
async def create_training_run(
    project_id: uuid.UUID,
    dataset_ref: str,
    tenant_id: str = Depends(get_tenant_id),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    config: dict = Body(default_factory=lambda: dict(ray_client.DEFAULT_CONFIG)),
    dataset_id: uuid.UUID | None = None,
    dataset_version: str | None = None,
    test_dataset_id: uuid.UUID | None = None,
    test_dataset_version: str | None = None,
    reproduction_group_id: uuid.UUID | None = None,
):
    # FR2 (04-api.md): idempotency key — a retried submission returns the
    # already-created run instead of double-submitting a GPU job.
    async with TenantConn(tenant_id) as conn:
        existing = await conn.fetchrow(
            "SELECT id, status, ray_job_id, config FROM training_runs "
            "WHERE project_id = $1 AND idempotency_key = $2",
            project_id,
            idempotency_key,
        )
        if existing:
            return dict(existing)

        # FR4 (07-compute-ray-k8s.md): hard quota, enforced at submission,
        # not just billed after the fact.
        inflight = await conn.fetchval(
            "SELECT count(*) FROM training_runs "
            "WHERE tenant_id = $1 AND status IN ('submitted', 'running')",
            tenant_id,
        )
        if inflight >= MAX_CONCURRENT:
            raise HTTPException(
                status_code=429,
                detail=f"tenant has {inflight} training runs in flight (max {MAX_CONCURRENT})",
            )

        # If a real dataset with uploaded files is referenced, the job
        # trains on those real files instead of a generated placeholder
        # set (see app/ray_client.py's Ultralytics template).
        # dataset_version pins to a specific LakeFS commit (see
        # app/lakefs_client.py / datasets.py's versions endpoints) for
        # reproducible training; unset trains on the branch's current
        # working state.
        file_keys = []
        if dataset_id is not None:
            dataset = await conn.fetchrow(
                "SELECT id FROM datasets WHERE id = $1 AND project_id = $2",
                dataset_id,
                project_id,
            )
            if dataset is None:
                raise HTTPException(status_code=404, detail="dataset not found")
            file_keys = lakefs_client.list_files(str(dataset_id), ref=dataset_version or "")

        # FR5 (03-dataset-governance.md): a project can designate a default
        # test dataset that pre-fills every run's picker; an explicit
        # test_dataset_id on this call still wins.
        if test_dataset_id is None:
            test_dataset_id = await conn.fetchval(
                "SELECT default_test_dataset_id FROM projects WHERE id = $1", project_id
            )

        # A separately versioned, user-designated held-out test set — any
        # dataset can serve as one (app/datasets.py has no special "test"
        # type; it's just which dataset you point at here). Skips the
        # internal random split entirely when set (see ray_client.py).
        test_file_keys = []
        if test_dataset_id is not None:
            test_dataset = await conn.fetchrow(
                "SELECT id FROM datasets WHERE id = $1 AND project_id = $2",
                test_dataset_id,
                project_id,
            )
            if test_dataset is None:
                raise HTTPException(status_code=404, detail="test dataset not found")
            test_file_keys = lakefs_client.list_files(str(test_dataset_id), ref=test_dataset_version or "")

        # FR3 (03-dataset-governance.md): >=10 labeled samples (dataset +
        # its paired test set combined) before a run can target it —
        # cheap since list_files above already fetched the full key list.
        if dataset_id is not None:
            labeled = [k for k in (*file_keys, *test_file_keys) if not k.startswith("_unlabeled/")]
            if len(labeled) < MIN_LABELED_SAMPLES:
                raise HTTPException(
                    status_code=422,
                    detail=f"{len(labeled)} / {MIN_LABELED_SAMPLES} minimum labeled samples",
                )

        # FR2/FR3 (06-project-settings-deployment.md): project-level compute
        # target/base-model defaults.
        settings = await conn.fetchval("SELECT settings FROM projects WHERE id = $1", project_id) or {}
        if "base_model" not in config and settings.get("base_model"):
            config = {**config, "base_model": settings["base_model"]}

        ray_job_id = await ray_client.submit_job(
            tenant_id=tenant_id,
            project_id=str(project_id),
            dataset_ref=dataset_ref,
            config=config,
            file_keys=file_keys,
            dataset_id=str(dataset_id) if dataset_id else None,
            dataset_version=dataset_version,
            test_file_keys=test_file_keys,
            test_dataset_id=str(test_dataset_id) if test_dataset_id else None,
            test_dataset_version=test_dataset_version,
            compute_target=settings.get("compute_target", "cpu"),
        )
        row = await conn.fetchrow(
            "INSERT INTO training_runs (tenant_id, project_id, idempotency_key, ray_job_id, dataset_ref, config, "
            "dataset_id, dataset_version, test_dataset_id, test_dataset_version, reproduction_group_id) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) "
            "RETURNING id, status, ray_job_id, config, dataset_id, dataset_version, test_dataset_id, "
            "test_dataset_version, reproduction_group_id",
            tenant_id,
            project_id,
            idempotency_key,
            ray_job_id,
            dataset_ref,
            config,
            dataset_id,
            dataset_version,
            test_dataset_id,
            test_dataset_version,
            reproduction_group_id,
        )
    return dict(row)


@router.get("")
async def list_training_runs(
    project_id: uuid.UUID,
    tenant_id: str = Depends(get_tenant_id),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    async with TenantConn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT id, status, dataset_ref, ray_job_id, mlflow_run_id, config, metrics, created_at, completed_at "
            "FROM training_runs WHERE project_id = $1 "
            "ORDER BY created_at DESC LIMIT $2 OFFSET $3",
            project_id,
            limit,
            offset,
        )
    return [dict(r) for r in rows]


@router.get("/{run_id}")
async def get_training_run(
    project_id: uuid.UUID, run_id: uuid.UUID, tenant_id: str = Depends(get_tenant_id)
):
    async with TenantConn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM training_runs WHERE id = $1 AND project_id = $2", run_id, project_id
        )
        if row is None:
            raise HTTPException(status_code=404, detail="training run not found")
        row = dict(row)

        if row["status"] in ("submitted", "running"):
            ray_status = await ray_client.get_status(row["ray_job_id"])
            new_status = _RAY_TO_STATUS.get(ray_status, row["status"])
            if new_status != row["status"]:
                row = await _transition(conn, row, new_status, tenant_id)
    return row


@router.get("/{run_id}/logs")
async def get_training_run_logs(
    project_id: uuid.UUID, run_id: uuid.UUID, tenant_id: str = Depends(get_tenant_id)
):
    """Live progress while a run is still submitted/running — not just the
    final result. Ray's job-logs endpoint returns whatever a still-running
    job has printed so far, and parse_metrics/parse_extra_metrics are plain
    line scanners that already work fine on a partial log (Ultralytics'
    own per-epoch console output shows up here as the job progresses, no
    change needed in the training templates)."""
    async with TenantConn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT ray_job_id, status FROM training_runs WHERE id = $1 AND project_id = $2", run_id, project_id
        )
    if row is None:
        raise HTTPException(status_code=404, detail="training run not found")

    logs = await ray_client.get_logs(row["ray_job_id"])
    return {
        "status": row["status"],
        "logs": logs[-8000:],
        "metrics": ray_client.parse_metrics(logs),
        "extra_metrics": ray_client.parse_extra_metrics(logs),
    }


@router.get("/{run_id}/lineage")
async def get_training_run_lineage(
    project_id: uuid.UUID, run_id: uuid.UUID, tenant_id: str = Depends(get_tenant_id)
):
    """FR1 (05-reproducibility-lineage.md): one endpoint joining dataset
    commit -> training run -> model registry entry -> active deployment,
    instead of four separate lookups a user has to correlate by hand."""
    async with TenantConn(tenant_id) as conn:
        run = await conn.fetchrow(
            "SELECT id, status, dataset_id, dataset_version, test_dataset_id, test_dataset_version, "
            "mlflow_run_id, reproduction_group_id, config, created_at FROM training_runs "
            "WHERE id = $1 AND project_id = $2",
            run_id,
            project_id,
        )
        if run is None:
            raise HTTPException(status_code=404, detail="training run not found")

        model = await conn.fetchrow(
            "SELECT id, mlflow_model_name, mlflow_version, stage FROM model_versions "
            "WHERE training_run_id = $1 ORDER BY created_at DESC LIMIT 1",
            run_id,
        )
        deployment = None
        if model is not None:
            deployment = await conn.fetchrow(
                "SELECT id, status, route_prefix FROM deployments WHERE training_run_id = $1 "
                "ORDER BY created_at DESC LIMIT 1",
                run_id,
            )

    commit_message = None
    if run["dataset_id"] is not None and run["dataset_version"]:
        versions = lakefs_client.list_versions(str(run["dataset_id"]))
        commit = next((v for v in versions if v.get("id") == run["dataset_version"]), None)
        if commit:
            commit_message = commit.get("message")

    return {
        "training_run": dict(run),
        "dataset_commit_message": commit_message,
        "mlflow_run_url": f"{mlflow_client.MLFLOW_URL}/#/experiments/0/runs/{run['mlflow_run_id']}"
        if run["mlflow_run_id"]
        else None,
        "model_version": dict(model) if model else None,
        "deployment": dict(deployment) if deployment else None,
    }


async def _transition(conn, row: dict, new_status: str, tenant_id: str) -> dict:
    """Handle a training run reaching (or moving toward) a terminal state:
    FR1 (06-ml-training): log to MLflow. FR1 (09-usage-metering): bill it,
    idempotently — this can run more than once for the same job across
    polls, record_usage's UNIQUE(resource_type, source_ref) absorbs that."""
    completed_at = datetime.now(timezone.utc) if new_status in ("succeeded", "failed") else None

    model_artifact = None  # only set below when freshly parsed; COALESCE in the UPDATE preserves any existing value
    metrics = None
    if new_status in ("succeeded", "failed") and row["mlflow_run_id"] is None:
        logs = await ray_client.get_logs(row["ray_job_id"])
        scalar_metrics = ray_client.parse_metrics(logs)
        # epoch_history/confusion_matrix are structured, not floats — MLflow's
        # log-metric API only accepts scalars, so they're stored on our own
        # row (below) but never sent to MLflow.
        metrics = {**scalar_metrics, **ray_client.parse_extra_metrics(logs)}
        model_artifact = ray_client.parse_model_artifact(logs)
        mlflow_run_id = await mlflow_client.log_training_run(
            tenant_id=tenant_id,
            project_id=str(row["project_id"]),
            dataset_ref=row["dataset_ref"],
            metrics=scalar_metrics,
            succeeded=(new_status == "succeeded"),
        )
        if new_status == "succeeded":
            elapsed = (completed_at - row["created_at"]).total_seconds()
            await usage.record_usage(
                conn,
                tenant_id=tenant_id,
                project_id=row["project_id"],
                resource_type="training",
                quantity=elapsed,
                unit="gpu_seconds",
                occurred_at=completed_at,
                source_ref=row["ray_job_id"],
            )
    else:
        mlflow_run_id = row["mlflow_run_id"]

    if new_status in ("succeeded", "failed", "cancelled"):
        app_metrics.training_runs_total.add(1, {"status": new_status})

    updated = await conn.fetchrow(
        "UPDATE training_runs SET status = $2, mlflow_run_id = $3, "
        "completed_at = COALESCE(completed_at, $4), model_artifact = COALESCE(model_artifact, $5), "
        "metrics = COALESCE(metrics, $6) "
        "WHERE id = $1 RETURNING *",
        row["id"],
        new_status,
        mlflow_run_id,
        completed_at,
        model_artifact,
        metrics,
    )
    return dict(updated)


async def reconcile_inflight_runs() -> None:
    """Poll Ray for every submitted/running run across all tenants, so a run
    nobody ever GETs individually still transitions out of 'submitted' —
    otherwise it sits there forever and keeps counting against the tenant's
    MAX_CONCURRENT_TRAINING_RUNS quota (see create_training_run)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        tenant_ids = [r["inflight_training_tenant_ids"] for r in await conn.fetch("SELECT inflight_training_tenant_ids()")]

    for tenant_id in tenant_ids:
        async with TenantConn(str(tenant_id)) as conn:
            rows = await conn.fetch(
                "SELECT * FROM training_runs WHERE tenant_id = $1 AND status IN ('submitted', 'running')",
                tenant_id,
            )
            for row in rows:
                row = dict(row)
                try:
                    ray_status = await ray_client.get_status(row["ray_job_id"])
                except Exception:  # noqa: BLE001 - one bad job shouldn't block the sweep
                    logger.exception("failed to poll ray status for run %s", row["id"])
                    continue
                new_status = _RAY_TO_STATUS.get(ray_status, row["status"])
                if new_status != row["status"]:
                    await _transition(conn, row, new_status, str(tenant_id))


@router.post("/{run_id}/cancel")
async def cancel_training_run(
    project_id: uuid.UUID, run_id: uuid.UUID, tenant_id: str = Depends(get_tenant_id)
):
    async with TenantConn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM training_runs WHERE id = $1 AND project_id = $2", run_id, project_id
        )
        if row is None:
            raise HTTPException(status_code=404, detail="training run not found")
        if row["status"] not in ("submitted", "running"):
            raise HTTPException(status_code=409, detail=f"run already {row['status']}")
        await ray_client.stop_job(row["ray_job_id"])
        updated = await conn.fetchrow(
            "UPDATE training_runs SET status = 'cancelled', completed_at = now() "
            "WHERE id = $1 RETURNING *",
            run_id,
        )
    return dict(updated)
