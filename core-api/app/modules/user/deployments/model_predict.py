"""POST /v1/predict (planning/24, sync only): resolve X-Model-Id / X-Model-Revision to the one live deployment of that model
version, forward `input` to Serve unchanged, answer with the envelope. The key (require_key) already fixed tenant and project."""
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from http import HTTPStatus

import httpx

from app.core import plan_limits, rate_limit
from app.core.authz import KeyContext
from app.core.metrics import inference as inference_metrics
from app.core.problem import Problem, not_found
from app.core.tenant_conn import TenantConn
from app.integrations import serve_client
from app.integrations.data_plane.mlflow import ModelStage
from app.modules.user.deployments import calls, service, shared
from app.modules.user.deployments.constants import (
    MODEL_REJECTED_DETAIL_MAX_CHARS,
    PREDICTION_ID_PREFIX,
    RATE_LIMIT_WINDOW_SECONDS,
    UPSTREAM_ERROR_DETAIL,
    UPSTREAM_TIMEOUT_DETAIL,
    ProblemCode,
    Serving,
)
from app.modules.user.deployments.schemas import PredictionEnvelope, PredictModelRef, PredictRequest, PredictUsage
from app.modules.user.deployments.status import DeploymentStatus
from app.modules.user.usage import UsageResource, UsageUnit, record_usage

logger = logging.getLogger(__name__)

# The newest live deployment of the model (a given revision, or any; the key's model allowlist was checked by require_key): an active one first, then a Production version,
# then the newest. ponytail: there is no promoted_at column, so "newest promotion wins" is "newest version".
_RESOLVE = (
    "SELECT d.id, d.status, d.serving, d.route_prefix, d.training_run_id, d.rate_limit_per_minute, mv.mlflow_version "
    "FROM models m JOIN model_versions mv ON mv.model_id = m.id "
    "JOIN deployments d ON d.model_version_id = mv.id AND d.status <> $4 "
    "WHERE m.id = $1 AND m.project_id = $2 AND ($3::text IS NULL OR mv.mlflow_version = $3) "
    "ORDER BY (d.status = $5) DESC, (mv.stage = $6) DESC, mv.created_at DESC, d.created_at DESC LIMIT 1"
)


def _from_serve(e: shared.ServeFailed) -> Problem:
    """Serve's own words are logged, never returned (except the model's refusal of the input, cut short)."""
    if e.code == serve_client.ServeErrorCode.BAD_INPUT:
        return Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.MODEL_REJECTED_INPUT, str(e.detail)[:MODEL_REJECTED_DETAIL_MAX_CHARS])
    logger.warning("serve failed (%s): %s", e.code, e.detail)
    if e.code == serve_client.ServeErrorCode.TIMEOUT:
        return Problem(HTTPStatus.GATEWAY_TIMEOUT, ProblemCode.UPSTREAM_TIMEOUT, UPSTREAM_TIMEOUT_DETAIL)
    return Problem(HTTPStatus.BAD_GATEWAY, ProblemCode.UPSTREAM_ERROR, UPSTREAM_ERROR_DETAIL)


async def _call_legacy(route_prefix: str, body) -> tuple[object, float]:
    """A deployment with a Serve app of its own: POST to its route; the status maps like a shared call's."""
    try:
        resp, elapsed_ms = await service._post_to_serve(route_prefix, body)
    except httpx.TimeoutException:
        raise Problem(HTTPStatus.GATEWAY_TIMEOUT, ProblemCode.UPSTREAM_TIMEOUT, UPSTREAM_TIMEOUT_DETAIL) from None
    except httpx.HTTPError:
        raise Problem(HTTPStatus.BAD_GATEWAY, ProblemCode.UPSTREAM_ERROR, UPSTREAM_ERROR_DETAIL) from None
    if resp.is_client_error:
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.MODEL_REJECTED_INPUT, resp.text[:MODEL_REJECTED_DETAIL_MAX_CHARS])
    if resp.is_error:
        logger.warning("legacy serve answered %s: %s", resp.status_code, resp.text[:MODEL_REJECTED_DETAIL_MAX_CHARS])
        raise Problem(HTTPStatus.BAD_GATEWAY, ProblemCode.UPSTREAM_ERROR, UPSTREAM_ERROR_DETAIL)
    return resp.json(), elapsed_ms


async def _rate_limited(deployment_id: uuid.UUID, limit: int) -> None:
    if not await rate_limit.check(str(deployment_id), limit):
        problem = Problem(HTTPStatus.TOO_MANY_REQUESTS, ProblemCode.RATE_LIMITED, f"rate limit exceeded ({limit}/min for this model)")
        problem.headers = {"Retry-After": str(RATE_LIMIT_WINDOW_SECONDS - int(time.time()) % RATE_LIMIT_WINDOW_SECONDS)}
        raise problem


async def predict(ctx: KeyContext, model_id: str, revision: str | None, request: PredictRequest) -> PredictionEnvelope:
    try:
        model_uuid = uuid.UUID(model_id)
    except ValueError:
        raise not_found() from None
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            _RESOLVE, model_uuid, ctx.project_id, revision, DeploymentStatus.DELETED, DeploymentStatus.ACTIVE, ModelStage.PRODUCTION)
        limits = await plan_limits.predict_limits(conn, ctx.tenant_id)
    if row is None:  # unknown model or revision, another project's, or one with no deployment: one body
        raise not_found()
    if row["status"] != DeploymentStatus.ACTIVE:  # deploying, stopped or failed: one code here (the legacy route has its own for stopped)
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.NOT_ACTIVE, f"the model's deployment is {row['status']}, not active")
    size = len(json.dumps(request.input))
    service._check_size(size, limits)
    deployment_id, run_id = row["id"], row["training_run_id"]
    await _rate_limited(deployment_id, row["rate_limit_per_minute"])

    prediction_id = uuid.uuid4()
    if row["serving"] == Serving.SHARED:
        try:
            prediction, elapsed_ms = await shared.answer(
                ctx.tenant_id, ctx.project_id, deployment_id, run_id, request.input, limits.max_inflight_per_deployment)
        except shared.ServeFailed as e:
            mapped = _from_serve(e)
            await calls.record(ctx.tenant_id, deployment_id, run_id, status=mapped.status_code, latency_ms=e.elapsed_ms, cold_load=None, error_code=e.code, request_bytes=size)
            raise mapped from e
        output, cold_load = {"predictions": prediction.predictions}, prediction.cold_load
        await calls.record(ctx.tenant_id, deployment_id, run_id, status=HTTPStatus.OK, latency_ms=elapsed_ms, cold_load=cold_load, error_code=None, request_bytes=size)
        inference_metrics.latency.record(elapsed_ms, {"deployment_id": str(deployment_id), "cold_load": cold_load})
    else:
        output, elapsed_ms = await _call_legacy(row["route_prefix"], request.input)
        inference_metrics.latency.record(elapsed_ms, {"deployment_id": str(deployment_id)})

    async with TenantConn(ctx.tenant_id) as conn:  # only a 2xx is metered, once per prediction id
        await record_usage(
            conn, tenant_id=ctx.tenant_id, project_id=ctx.project_id, resource_type=UsageResource.INFERENCE, quantity=1,
            unit=UsageUnit.REQUESTS, occurred_at=datetime.now(timezone.utc), source_ref=str(prediction_id))
    return PredictionEnvelope(
        id=f"{PREDICTION_ID_PREFIX}{prediction_id}", model=PredictModelRef(id=model_uuid, revision=row["mlflow_version"]), output=output,
        usage=PredictUsage(latency_ms=elapsed_ms), metadata=request.metadata, created_at=datetime.now(timezone.utc))
