"""Answering a request with a deployment's model on the shared predictor apps (serve_client.predict).

`call` is the one Serve call every entrance makes: `answer` (the in-app test call, service.test_deployment, session auth),
the per-deployment-key route (`authenticate` + `serve_authenticated` below) and POST /v1/predict (model_predict.py). Only
their auth and bookkeeping differ.

The model's URLs are looked up once per (deployment, run) and remembered, so a request costs one small query, not an MLflow
round trip. A `main` re-deploy changes the run, so the next request builds the new entry and the old one ages out.
"""
import hashlib
import json
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus

from pydantic import BaseModel

from app.core import plan_limits, rate_limit, task_types
from app.core.authz import TenantStatus
from app.core.db import get_pool
from app.core.metrics import inference as inference_metrics
from app.core.problem import Problem, ProblemCode as KernelProblemCode
from app.core.tenant_conn import TenantConn
from app.integrations import serve_client
from app.modules.user.deployments import calls
from app.modules.user.deployments.constants import (
    DEFAULT_COMPUTE_TARGET,
    METADATA_FIELD,
    PREDICTIONS_FIELD,
    RATE_LIMIT_WINDOW_SECONDS,
    RETRY_AFTER_HEADER,
    SERVE_ERROR_STATUS,
    SETTING_COMPUTE_TARGET,
    STOPPED_DETAIL,
    TARGET_CACHE_SIZE,
    TOO_MANY_REQUESTS_DETAIL,
    ProblemCode,
)
from app.modules.user.deployments.status import DeploymentStatus
from app.modules.user.projects import get_settings, get_task_type
from app.modules.user.training import get_run_model
from app.modules.user.usage import UsageResource, UsageUnit, record_usage

_NO_MODEL = "the deployment's run has no servable model"


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def require_active(status: str) -> None:
    """409 unless the deployment serves; a stopped one says so by its own code (its predict key stays valid)."""
    if status == DeploymentStatus.STOPPED:
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.STOPPED, STOPPED_DETAIL)
    if status != DeploymentStatus.ACTIVE:
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.NOT_ACTIVE, f"deployment is {status}, not active")


def check_size(size: int, limits: plan_limits.Predict) -> None:
    """The plan's body cap, which is at most the ceiling the route already refused above before parsing."""
    if size > limits.max_body_bytes:
        raise Problem(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, ProblemCode.BODY_TOO_LARGE, f"the body is over {limits.max_body_bytes} bytes (this plan's limit)")


class ServeFailed(Problem):
    """A call that reached Serve and failed: the status for its code, and what predict_calls logs about it."""

    def __init__(self, code: serve_client.ServeErrorCode, message: str, elapsed_ms: float, run_id: uuid.UUID, request_bytes: int):
        super().__init__(SERVE_ERROR_STATUS[code], code, f"{code}: {message}")
        self.elapsed_ms, self.run_id, self.request_bytes = elapsed_ms, run_id, request_bytes


class Answered(BaseModel):
    run_id: uuid.UUID
    request_bytes: int
    prediction: serve_client.Prediction
    elapsed_ms: float


@dataclass(frozen=True)
class Target:
    app: serve_client.ComputeTarget
    run_id: uuid.UUID
    model: serve_client.ServedModel


_targets: OrderedDict[tuple[uuid.UUID, uuid.UUID], Target] = OrderedDict()
_inflight: dict[uuid.UUID, int] = {}  # per process: the cap is per core-api replica, not global


async def _target(tenant_id: str, project_id: uuid.UUID, deployment_id: uuid.UUID, run_id: uuid.UUID) -> Target:
    if (found := _targets.get((deployment_id, run_id))) is not None:
        _targets.move_to_end((deployment_id, run_id))
        return found
    async with TenantConn(tenant_id) as conn:
        run = await get_run_model(conn, project_id, run_id)
        task = await get_task_type(conn, project_id)
        info = None if task is None else await task_types.get(conn, task)
        settings_ = await get_settings(conn, project_id)
    if run is None or not run.servable or info is None or not info.servable:
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.NO_SERVABLE_MODEL, _NO_MODEL)
    model = await serve_client.served_model(info, run.mlflow_run_id, run.class_names, tenant_id, str(project_id))
    target = Target(serve_client.ComputeTarget(settings_.get(SETTING_COMPUTE_TARGET, DEFAULT_COMPUTE_TARGET)), run_id, model)
    _targets[(deployment_id, run_id)] = target
    while len(_targets) > TARGET_CACHE_SIZE:
        _targets.popitem(last=False)
    return target


@contextmanager
def _slot(deployment_id: uuid.UUID, limit: int):
    if _inflight.get(deployment_id, 0) >= limit:
        raise Problem(HTTPStatus.TOO_MANY_REQUESTS, ProblemCode.TOO_MANY_INFLIGHT, TOO_MANY_REQUESTS_DETAIL)
    _inflight[deployment_id] = _inflight.get(deployment_id, 0) + 1
    try:
        yield
    finally:
        _inflight[deployment_id] -= 1
        if not _inflight[deployment_id]:
            del _inflight[deployment_id]


async def call(
    tenant_id: str, project_id: uuid.UUID, deployment_id: uuid.UUID, run_id: uuid.UUID, body, size: int, max_inflight: int,
) -> Answered:
    """Answer `body` (`size` bytes as JSON) with `run_id`'s model, at most `max_inflight` at once for the deployment (429 over
    it), 409 without a servable model; ServeFailed (a Problem with the status SERVE_ERROR_STATUS gives its code) on a failed call."""
    target = await _target(tenant_id, project_id, deployment_id, run_id)
    with _slot(deployment_id, max_inflight):
        start = time.perf_counter()
        try:
            prediction = await serve_client.predict(target.app, str(deployment_id), str(target.run_id), target.model, body)
        except serve_client.ServeCallError as e:
            raise ServeFailed(e.code, e.message, (time.perf_counter() - start) * 1000, run_id, size) from e
    return Answered(run_id=run_id, request_bytes=size, prediction=prediction, elapsed_ms=(time.perf_counter() - start) * 1000)


async def answer(tenant_id: str, project_id: uuid.UUID, deployment_id: uuid.UUID, body: dict) -> Answered:
    """Answer `body` with the run the deployment serves now, under the tenant plan's body (413) and in-flight caps (`call`).
    No connection is held across the Serve call."""
    async with TenantConn(tenant_id) as conn:
        run_id = await conn.fetchval("SELECT training_run_id FROM deployments WHERE id = $1", deployment_id)
        limits = await plan_limits.predict_limits(conn, tenant_id)
    size = len(json.dumps(body))
    check_size(size, limits)
    return await call(tenant_id, project_id, deployment_id, run_id, body, size, limits.max_inflight_per_deployment)


async def authenticate(deployment_id: uuid.UUID, predict_key: str):
    """The deployment row for a valid predict key, or the Problem that refuses the call (401, suspended tenant, not active, 429). Runs
    before the route reads the body, so an unauthenticated caller makes it buffer nothing."""
    # req/04: "separate auth scope from management API — this is what the
    # customer's own downstream systems call." Genuinely separate: no
    # tenant/JWT credential accepted here at all, just the per-deployment
    # secret handed back once at deploy time (see create_deployment). The
    # lookup runs SECURITY DEFINER (deployment_for_predict() in the init migration) so this path
    # never needs a tenant session context.
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM deployment_for_predict($1, $2)", deployment_id, hash_key(predict_key))
    if row is None:
        raise Problem(HTTPStatus.UNAUTHORIZED, ProblemCode.INVALID_PREDICT_KEY, "invalid deployment id or predict key")
    if row["tenant_status"] != TenantStatus.ACTIVE:
        raise Problem(HTTPStatus.SERVICE_UNAVAILABLE, KernelProblemCode.TENANT_SUSPENDED, "this tenant is not active")
    require_active(row["status"])
    if not await rate_limit.check(str(deployment_id), row["rate_limit_per_minute"]):
        problem = Problem(
            HTTPStatus.TOO_MANY_REQUESTS, ProblemCode.RATE_LIMITED,
            f"rate limit exceeded ({row['rate_limit_per_minute']}/min for this deployment)",
        )
        problem.headers = {RETRY_AFTER_HEADER: str(RATE_LIMIT_WINDOW_SECONDS - int(time.time()) % RATE_LIMIT_WINDOW_SECONDS)}
        raise problem
    return row


async def predict(deployment_id: uuid.UUID, predict_key: str, body: dict) -> dict:
    return await serve_authenticated(await authenticate(deployment_id, predict_key), deployment_id, body)


async def serve_authenticated(row, deployment_id: uuid.UUID, body: dict) -> dict:
    """The per-deployment-key call once `authenticate` let it through: answered, logged in predict_calls, metered."""
    # FR6: an optional `metadata` passthrough field, popped before
    # forwarding to Serve (the model was never trained to expect it) and
    # echoed back alongside the prediction.
    metadata = body.pop(METADATA_FIELD, None)

    tenant_id = str(row["tenant_id"])
    try:
        done = await answer(tenant_id, row["project_id"], deployment_id, body)
    except ServeFailed as e:
        await calls.record(tenant_id, deployment_id, e.run_id, status=e.status_code, latency_ms=e.elapsed_ms, cold_load=None, error_code=e.code, request_bytes=e.request_bytes)
        raise
    inference_metrics.latency.record(done.elapsed_ms, {"deployment_id": str(deployment_id), "cold_load": done.prediction.cold_load})
    await calls.record(
        tenant_id, deployment_id, done.run_id, status=HTTPStatus.OK, latency_ms=done.elapsed_ms, cold_load=done.prediction.cold_load,
        error_code=None, request_bytes=done.request_bytes)
    result = {PREDICTIONS_FIELD: done.prediction.predictions}

    # FR2 (09-usage-billing-two-tier.md): meter inference — request id in
    # source_ref (not just deployment_id) so the existing
    # UNIQUE(resource_type, source_ref) idempotency key doesn't collapse
    # distinct requests into one row.
    async with TenantConn(tenant_id) as usage_conn:
        await record_usage(
            usage_conn,
            tenant_id=tenant_id,
            project_id=row["project_id"],
            resource_type=UsageResource.INFERENCE,
            quantity=1,
            unit=UsageUnit.REQUESTS,
            occurred_at=datetime.now(timezone.utc),
            source_ref=f"{deployment_id}:{uuid.uuid4()}",
        )

    if metadata is not None:
        result[METADATA_FIELD] = metadata
    return result
