"""HTTP surface for training runs: parse, authorize, call run_service, shape the response.
Domain transitions live in app/modules/user/training/service.py, which the background poller shares.
"""
import uuid

from fastapi import APIRouter, Depends, Query

from app.core.authz import Action, AuthzContext, require
from app.core.problem import Problem
from app.integrations import compute
from app.modules.user.training import log_parsing, run_service
from app.modules.user.training.constants import LOG_TAIL_CHARS
from app.modules.user.training.schemas import TrainingRun, TrainingRunLineage, TrainingRunLogs, TrainingRunSummary

NOT_IMPLEMENTED = "not_implemented"

router = APIRouter(prefix="/v1/projects/{project_id}/training-runs", tags=["training"])


@router.post("", status_code=202)
async def create_training_run(
    project_id: uuid.UUID,
    ctx: AuthzContext = Depends(require(Action.JOB_CREATE)),
):
    # ponytail: data access is stubbed until the trainer slice rewires training to dataset version ids
    # (docs/datasets-plan.md, Deferred); authz above still applies.
    raise Problem(501, NOT_IMPLEMENTED, "training is not available until the trainer slice")


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
    """Live progress while a run is still submitted/running — not just the
    final result. Ray's job-logs endpoint returns whatever a still-running
    job has printed so far, and parse_metrics/parse_extra_metrics are plain
    line scanners that already work fine on a partial log (Ultralytics'
    own per-epoch console output shows up here as the job progresses, no
    change needed in the training templates)."""
    ref = await run_service.get_log_ref(ctx, project_id, run_id)
    logs = await compute.logs(ref.ray_job_id)
    return {
        "status": ref.status,
        "logs": logs[-LOG_TAIL_CHARS:],
        "metrics": log_parsing.parse_metrics(logs),
        "extra_metrics": log_parsing.parse_extra_metrics(logs),
    }


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
