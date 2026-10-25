"""Deployments HTTP surface: parse, authorize, call the service, shape the response."""
import uuid

from fastapi import APIRouter, Body, Depends, Header

from app.core.authz import Action, AuthzContext, public, require
from app.modules.user.deployments import service
from app.modules.user.deployments.constants import PREDICT_KEY_HEADER, TEST_CALLS_DEFAULT_LIMIT
from app.modules.user.deployments.schemas import (
    DeploymentCreated,
    DeploymentDetail,
    DeploymentListItem,
    TestCall,
    TestCallResult,
)

router = APIRouter(tags=["deployments"])


@router.post(
    "/v1/projects/{project_id}/deployments", status_code=202, response_model=DeploymentCreated, response_model_exclude_none=True
)
async def create_deployment(
    project_id: uuid.UUID,
    training_run_id: uuid.UUID,
    ctx: AuthzContext = Depends(require(Action.DEPLOYMENT_MANAGE)),
):
    return await service.create_deployment(ctx, project_id, training_run_id)


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


@router.post("/v1/projects/{project_id}/deployments/{deployment_id}/test", response_model=TestCallResult)
async def test_deployment(
    project_id: uuid.UUID,
    deployment_id: uuid.UUID,
    body: dict = Body(...),
    ctx: AuthzContext = Depends(require(Action.DEPLOYMENT_TRY)),
):
    return await service.test_deployment(ctx, project_id, deployment_id, body)


@router.get("/v1/projects/{project_id}/deployments/{deployment_id}/test-calls", response_model=list[TestCall])
async def list_test_calls(
    project_id: uuid.UUID,
    deployment_id: uuid.UUID,
    ctx: AuthzContext = Depends(require(Action.DEPLOYMENT_READ)),
    limit: int = TEST_CALLS_DEFAULT_LIMIT,
):
    return await service.list_test_calls(ctx, project_id, deployment_id, limit)


# Key-based, not a user session: authenticated by the per-deployment predict key (planning/05).
@router.post("/v1/predict/{deployment_id}", dependencies=[Depends(public)])
async def predict(
    deployment_id: uuid.UUID,
    body: dict = Body(...),
    x_predict_key: str = Header(..., alias=PREDICT_KEY_HEADER),
):
    return await service.predict(deployment_id, x_predict_key, body)
