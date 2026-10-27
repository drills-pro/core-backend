"""HTTP surface for training runs: parse, authorize, call run_service, shape the response.
Domain transitions live in app/modules/user/training/service.py, which the background poller shares.
"""
import uuid

from fastapi import APIRouter, Depends, Header, Query

from app.core.authz import Action, AuthzContext, require
from app.integrations import compute
from app.modules.user.training import create_service, run_service
from app.modules.user.training.constants import IDEMPOTENCY_HEADER, LOG_TAIL_CHARS
from app.modules.user.training.schemas import (
    TrainingRun,
    TrainingRunCreate,
    TrainingRunLineage,
    TrainingRunLogs,
    TrainingRunSummary,
)

router = APIRouter(prefix="/v1/projects/{project_id}/training-runs", tags=["training"])


@router.post("", status_code=202, response_model=TrainingRun)
async def create_training_run(
    project_id: uuid.UUID,
    body: TrainingRunCreate,
    idempotency_key: str = Header(alias=IDEMPOTENCY_HEADER, min_length=1, max_length=255),
    ctx: AuthzContext = Depends(require(Action.JOB_CREATE)),
):
    """Train on one version of one of this project's datasets. Sending the same Idempotency-Key and body again returns the same run."""
    return await create_service.create_run(ctx, project_id, body, idempotency_key)


@router.get("", response_model=list[TrainingRunSummary])
async def list_training_runs(
    project_id: uuid.UUID,
    ctx: AuthzContext = Depends(require(Action.JOB_READ)),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    return await run_service.list_runs(ctx, project_id, limit, offset)


@router.get("/{run_id}", response_model=TrainingRun)
async def get_training_run(
    project_id: uuid.UUID, run_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.JOB_READ))
):
    # Read-only: status sync is the background reconciler's job (service.py).
    return await run_service.get_run(ctx, project_id, run_id)


@router.get("/{run_id}/logs", response_model=TrainingRunLogs)
async def get_training_run_logs(
    project_id: uuid.UUID, run_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.JOB_READ))
):
    """The end of the job's raw output, for debugging. Results come from the job's report (the run's `report`), not from here."""
    ref = await run_service.get_log_ref(ctx, project_id, run_id)
    logs = await compute.logs(ref.ray_job_id)
    return {"status": ref.status, "logs": logs[-LOG_TAIL_CHARS:]}


@router.get("/{run_id}/lineage", response_model=TrainingRunLineage)
async def get_training_run_lineage(
    project_id: uuid.UUID, run_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.JOB_READ))
):
    """FR1 (05-reproducibility-lineage.md): one endpoint joining dataset
    commit -> training run -> model registry entry -> active deployment,
    instead of four separate lookups a user has to correlate by hand."""
    return await run_service.get_lineage(ctx, project_id, run_id)


@router.post("/{run_id}/cancel", response_model=TrainingRun)
async def cancel_training_run(
    project_id: uuid.UUID, run_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.JOB_CANCEL))
):
    return await run_service.cancel_run(ctx, project_id, run_id)
