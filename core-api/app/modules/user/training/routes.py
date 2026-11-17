"""HTTP surface for training runs: parse, authorize, call run_service, shape the response.
Domain transitions live in app/modules/user/training/service.py, which the background poller shares.
"""
import uuid
from http import HTTPStatus

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import ValidationError

from app.core.authz import Action, AuthzContext, public, require
from app.core.problem import Problem
from app.core.idempotency import IdempotencyKey
from app.modules.user.model_registry import MODEL_NAME_PATTERN, ModelVersion
from app.modules.user.training import create_service, events_service, preset_service, run_service
from app.modules.user.training.constants import MAX_EVENT_BODY_BYTES, ProblemCode
from app.modules.user.training.schemas import (
    EventAck,
    PresetSummary,
    ProgressEvent,
    TrainingRun,
    TrainingRunCreate,
    TrainingRunSteps,
    TrainingRunSummary,
)

router = APIRouter(prefix="/v1/projects/{project_id}/training-runs", tags=["training"])
presets_router = APIRouter(prefix="/v1/projects/{project_id}/training-presets", tags=["training"])
# The trainer's channel: public() because no user session is involved; the per-run token is checked by `run_tenant`.
events_router = APIRouter(prefix="/internal/v1/training-runs", tags=["training-internal"], dependencies=[Depends(public)])
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


@router.get("/{run_id}/steps", response_model=TrainingRunSteps)
async def get_training_run_steps(
    project_id: uuid.UUID, run_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.JOB_READ))
):
    """The run's steps and, if it failed, a failure code: what a customer sees instead of the job's raw log."""
    return await run_service.get_steps(ctx, project_id, run_id)


@router.post("/{run_id}/cancel", response_model=TrainingRun)
async def cancel_training_run(
    project_id: uuid.UUID, run_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.JOB_CANCEL))
):
    return await run_service.cancel_run(ctx, project_id, run_id)


async def run_tenant(run_id: uuid.UUID, authorization: str | None = Header(default=None)) -> str:
    return await events_service.authenticate(run_id, authorization)


async def event_body(request: Request) -> ProgressEvent:
    """The request body, read up to MAX_EVENT_BODY_BYTES and no further, then validated. Parsed here and not by FastAPI because
    its 422 echoes the offending value, and a NaN or inf cannot be serialised back."""
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_EVENT_BODY_BYTES:
            raise Problem(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, ProblemCode.EVENT_TOO_LARGE, "event too large")
    try:
        return ProgressEvent.model_validate_json(raw)
    except ValidationError as e:
        detail = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors(include_url=False, include_context=False, include_input=False))
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.INVALID_EVENT, detail) from None


@events_router.post(
    "/{run_id}/events", status_code=201, response_model=EventAck,
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": ProgressEvent.model_json_schema()}}}},
)
async def post_training_run_event(
    run_id: uuid.UUID, response: Response, tenant_id: str = Depends(run_tenant), body: ProgressEvent = Depends(event_body)
):
    """A trainer's progress after an epoch. Authenticated by the run's token; only a running run takes events. A `seq` the
    run already has answers 200 and changes nothing."""
    ack = await events_service.record(tenant_id, run_id, body)
    if ack.duplicate:
        response.status_code = HTTPStatus.OK
    return ack
