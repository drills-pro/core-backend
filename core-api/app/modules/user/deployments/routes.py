"""Deployments HTTP surface: parse, authorize, call the service, shape the response."""
import json
import uuid
from http import HTTPStatus

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.core.authz import Action, AuthzContext, KeyContext, Scope, public, require, require_key
from app.core.idempotency import IDEMPOTENCY_HEADER, IDEMPOTENCY_KEY_MAX_LENGTH, IdempotencyKey
from app.core.problem import Problem, ProblemCode as KernelProblemCode
from app.core.settings import settings
from app.modules.user.deployments import lineage, model_predict, service
from app.modules.user.deployments.constants import (
    MODEL_ID_HEADER,
    IDEMPOTENCY_KEY_PATTERN,
    MODEL_REVISION_HEADER,
    PREDICT_KEY_HEADER,
    TEST_CALLS_DEFAULT_LIMIT,
    TEST_CALLS_MAX_LIMIT,
    TEST_CALLS_MIN_LIMIT,
    ProblemCode,
)
from app.modules.user.deployments.schemas import (
    DeploymentCreated,
    DeploymentDetail,
    DeploymentListItem,
    PredictionEnvelope,
    PredictRequest,
    TrainingRunLineage,
    TestCall,
    TestCallResult,
)

router = APIRouter(tags=["deployments"])


async def capped_body(request: Request) -> dict:
    """The JSON object the caller sent, refused with 413 above serving.max_body_bytes (the declared length is checked before reading)."""
    limit = settings.serving.max_body_bytes
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise Problem(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, ProblemCode.BODY_TOO_LARGE, f"the body is over {limit} bytes")
    data = await request.body()
    if len(data) > limit:
        raise Problem(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, ProblemCode.BODY_TOO_LARGE, f"the body is over {limit} bytes")
    try:
        body = json.loads(data)
    except ValueError:
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.BODY_NOT_JSON, "the body is not JSON") from None
    if not isinstance(body, dict):
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.BODY_NOT_OBJECT, "the body must be a JSON object")
    return body


@router.post(
    "/v1/projects/{project_id}/deployments", status_code=202, response_model=DeploymentCreated, response_model_exclude_none=True
)
async def create_deployment(
    project_id: uuid.UUID,
    training_run_id: uuid.UUID,
    idempotency_key: IdempotencyKey,
    ctx: AuthzContext = Depends(require(Action.DEPLOYMENT_MANAGE)),
):
    """Deploy a training run. Sending the same Idempotency-Key and training_run_id again returns that deployment (without
    the predict key, which is shown once); the same key with another run is 409."""
    return await service.create_deployment(ctx, project_id, training_run_id, idempotency_key)


@router.get("/v1/projects/{project_id}/deployments", response_model=list[DeploymentListItem])
async def list_deployments(project_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.DEPLOYMENT_READ))):
    return await service.list_deployments(ctx, project_id)


@router.get("/v1/projects/{project_id}/deployments/{deployment_id}", response_model=DeploymentDetail)
async def get_deployment(
    project_id: uuid.UUID,
    deployment_id: uuid.UUID,
    ctx: AuthzContext = Depends(require(Action.DEPLOYMENT_READ)),
):
    return await service.get_deployment(ctx, project_id, deployment_id)


@router.post("/v1/projects/{project_id}/deployments/{deployment_id}/stop", response_model=DeploymentDetail)
async def stop_deployment(
    project_id: uuid.UUID,
    deployment_id: uuid.UUID,
    ctx: AuthzContext = Depends(require(Action.DEPLOYMENT_MANAGE)),
):
    """Stop serving: predict and try-it answer 409, the predict key is kept. Repeating it is fine."""
    return await service.stop_deployment(ctx, project_id, deployment_id)


@router.post("/v1/projects/{project_id}/deployments/{deployment_id}/start", response_model=DeploymentDetail)
async def start_deployment(
    project_id: uuid.UUID,
    deployment_id: uuid.UUID,
    ctx: AuthzContext = Depends(require(Action.DEPLOYMENT_MANAGE)),
):
    """Serve a stopped deployment again: same row, same predict key."""
    return await service.start_deployment(ctx, project_id, deployment_id)


@router.delete("/v1/projects/{project_id}/deployments/{deployment_id}", status_code=204)
async def delete_deployment(
    project_id: uuid.UUID,
    deployment_id: uuid.UUID,
    ctx: AuthzContext = Depends(require(Action.DEPLOYMENT_MANAGE)),
):
    """Soft delete: the row stays for lineage, the predict key is cleared and the deployment leaves the list."""
    await service.delete_deployment(ctx, project_id, deployment_id)


@router.get("/v1/projects/{project_id}/training-runs/{run_id}/lineage", response_model=TrainingRunLineage)
async def get_training_run_lineage(
    project_id: uuid.UUID, run_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.JOB_READ))
):
    """One endpoint joining dataset commit -> training run -> model registry entry -> active deployment."""
    return await lineage.get_lineage(ctx, project_id, run_id)


@router.post("/v1/projects/{project_id}/deployments/{deployment_id}/test", response_model=TestCallResult)
async def test_deployment(
    project_id: uuid.UUID,
    deployment_id: uuid.UUID,
    body: dict = Depends(capped_body),
    ctx: AuthzContext = Depends(require(Action.DEPLOYMENT_TRY)),
):
    return await service.test_deployment(ctx, project_id, deployment_id, body)


@router.get("/v1/projects/{project_id}/deployments/{deployment_id}/test-calls", response_model=list[TestCall])
async def list_test_calls(
    project_id: uuid.UUID,
    deployment_id: uuid.UUID,
    ctx: AuthzContext = Depends(require(Action.DEPLOYMENT_READ)),
    limit: int = Query(TEST_CALLS_DEFAULT_LIMIT, ge=TEST_CALLS_MIN_LIMIT, le=TEST_CALLS_MAX_LIMIT),
):
    return await service.list_test_calls(ctx, project_id, deployment_id, limit)


# Key-based, not a user session: authenticated by the per-deployment predict key (planning/05).
@router.post("/v1/predict/{deployment_id}", dependencies=[Depends(public)])
async def predict(
    deployment_id: uuid.UUID,
    body: dict = Depends(capped_body),
    x_predict_key: str = Header(..., alias=PREDICT_KEY_HEADER),
):
    return await service.predict(deployment_id, x_predict_key, body)


# Key-authenticated (a drl_ project key with the predict scope): the model is named by X-Model-Id [+ X-Model-Revision].
@router.post("/v1/predict", response_model=PredictionEnvelope)
async def predict_model(
    ctx: KeyContext = Depends(require_key(Scope.PREDICT)),
    body: dict = Depends(capped_body),
    x_model_id: str = Header(..., alias=MODEL_ID_HEADER),
    x_model_revision: str | None = Header(None, alias=MODEL_REVISION_HEADER),
    idempotency_key: str | None = Header(None, alias=IDEMPOTENCY_HEADER, min_length=1, max_length=IDEMPOTENCY_KEY_MAX_LENGTH, pattern=IDEMPOTENCY_KEY_PATTERN),
):
    try:
        request = PredictRequest.model_validate(body)
    except ValidationError as e:
        detail = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, KernelProblemCode.VALIDATION_ERROR, detail) from None
    outcome = await model_predict.predict(ctx, x_model_id, x_model_revision, request, idempotency_key=idempotency_key)
    return JSONResponse(outcome.envelope.model_dump(mode="json"), outcome.status_code, outcome.headers)
