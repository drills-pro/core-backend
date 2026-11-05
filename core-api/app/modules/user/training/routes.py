"""HTTP surface for training runs: parse, authorize, call run_service, shape the response.
Domain transitions live in app/modules/user/training/service.py, which the background poller shares.
"""
import uuid

from fastapi import APIRouter, Depends, Query

from app.core.authz import Action, AuthzContext, require
from app.core.idempotency import IdempotencyKey
from app.modules.user.model_registry import MODEL_NAME_PATTERN, ModelVersion
from app.modules.user.training import create_service, preset_service, run_service
from app.modules.user.training.schemas import (
    PresetSummary,
    TrainingRun,
    TrainingRunCreate,
    TrainingRunLogs,
    TrainingRunSummary,
)

router = APIRouter(prefix="/v1/projects/{project_id}/training-runs", tags=["training"])
presets_router = APIRouter(prefix="/v1/projects/{project_id}/training-presets", tags=["training"])
# Registering a run's model is training's endpoint (it reads the run) under the registry's URL space.
models_router = APIRouter(prefix="/v1/projects/{project_id}/models", tags=["models"])


@models_router.post("/{run_id}/register", status_code=201, response_model=ModelVersion)
async def register_model(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    name: str = Query(pattern=MODEL_NAME_PATTERN),
    ctx: AuthzContext = Depends(require(Action.MODEL_WRITE)),
):
    return await run_service.register_model(ctx, project_id, run_id, name)


@presets_router.get("", response_model=list[PresetSummary])
async def list_training_presets(project_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.JOB_READ))):
    """The presets this project's task type can train with: the names a run's `preset` takes, and their bounds."""
    return await preset_service.list_for_project(ctx, project_id)


@router.post("", status_code=202, response_model=TrainingRun)
async def create_training_run(
    project_id: uuid.UUID,
    body: TrainingRunCreate,
    idempotency_key: IdempotencyKey,
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
    return await run_service.get_logs(ctx, project_id, run_id)


@router.post("/{run_id}/cancel", response_model=TrainingRun)
async def cancel_training_run(
    project_id: uuid.UUID, run_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.JOB_CANCEL))
):
    return await run_service.cancel_run(ctx, project_id, run_id)
