"""POST /v1/predict (planning/24): resolve X-Model-Id / X-Model-Revision to the one live deployment of that model version, forward
`input` to Serve unchanged, answer with the envelope. The key (require_key) already fixed tenant and project. Three ways through:
a plain sync call, a sync call with an Idempotency-Key (stored, replayable), and a background one (stored, run by the worker)."""
import asyncio
import hashlib
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
from app.core.problem import Problem, ProblemCode as KernelProblemCode, not_found
from app.core.settings import settings
from app.core.tenant_conn import TenantConn
from app.integrations import serve_client
from app.integrations.data_plane.mlflow import ModelStage
from app.modules.user.deployments import calls, prediction_store as store, service, shared
from app.modules.user.deployments.constants import (
    ABANDONED_DETAIL,
    IDEMPOTENCY_KEY_REQUIRED_DETAIL,
    IDEMPOTENCY_REUSE_DETAIL,
    IN_PROGRESS_DETAIL,
    IN_PROGRESS_POLL_SECONDS,
    IN_PROGRESS_RETRY_AFTER_SECONDS,
    LOCATION_HEADER,
    MODEL_REJECTED_DETAIL_MAX_CHARS,
    PREDICT_PATH,
    PREDICTION_ID_PREFIX,
    PREFER_RESPOND_ASYNC,
    PREFERENCE_APPLIED_HEADER,
    RATE_LIMIT_WINDOW_SECONDS,
    RETRY_AFTER_HEADER,
    STORED_ERROR_STATUS,
    TERMINAL_STATUSES,
    UPSTREAM_ERROR_DETAIL,
    UPSTREAM_TIMEOUT_DETAIL,
    PredictionMode,
    PredictionStatus,
    ProblemCode,
    Serving,
)
from app.modules.user.deployments.schemas import (
    PredictionEnvelope,
    PredictionError,
    PredictModelRef,
    PredictOutcome,
    PredictRequest,
    PredictUsage,
)
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


def serve_problem(e: shared.ServeFailed) -> Problem:
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
        problem.headers = {RETRY_AFTER_HEADER: str(RATE_LIMIT_WINDOW_SECONDS - int(time.time()) % RATE_LIMIT_WINDOW_SECONDS)}
        raise problem


async def serve_call(tenant_id: str, project_id: uuid.UUID, d, input_, size: int, max_inflight: int) -> tuple[object, float]:
    """(output, milliseconds) of one call to the deployment `d` (id, serving, route_prefix, training_run_id); a Problem when it fails.
    A failure that reached Serve has one of STORED_ERROR_STATUS's codes; anything else (no servable model, too many in flight) did not."""
    deployment_id, run_id = d["id"], d["training_run_id"]
    if d["serving"] != Serving.SHARED:
        output, elapsed_ms = await _call_legacy(d["route_prefix"], input_)
        inference_metrics.latency.record(elapsed_ms, {"deployment_id": str(deployment_id)})
        return output, elapsed_ms
    try:
        prediction, elapsed_ms = await shared.answer(tenant_id, project_id, deployment_id, run_id, input_, max_inflight)
    except shared.ServeFailed as e:
        mapped = serve_problem(e)
        await calls.record(tenant_id, deployment_id, run_id, status=mapped.status_code, latency_ms=e.elapsed_ms, cold_load=None, error_code=e.code, request_bytes=size)
        raise mapped from e
    await calls.record(tenant_id, deployment_id, run_id, status=HTTPStatus.OK, latency_ms=elapsed_ms, cold_load=prediction.cold_load, error_code=None, request_bytes=size)
    inference_metrics.latency.record(elapsed_ms, {"deployment_id": str(deployment_id), "cold_load": prediction.cold_load})
    return {"predictions": prediction.predictions}, elapsed_ms


def request_hash(request: PredictRequest, model_id: uuid.UUID, revision: str | None) -> str:
    """What makes two requests "the same" for an Idempotency-Key: input, metadata and the model headers, as canonical JSON."""
    canonical = json.dumps(
        {"input": request.input, "metadata": request.metadata, "model": str(model_id), "revision": revision}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _outcome(row) -> PredictOutcome:
    """The answer for a stored prediction: a background one that is not finished is 202 with its Location, anything else 200."""
    if row["mode"] == PredictionMode.BACKGROUND and row["status"] not in TERMINAL_STATUSES:
        headers = {LOCATION_HEADER: f"{PREDICT_PATH}/{PREDICTION_ID_PREFIX}{row['id']}", PREFERENCE_APPLIED_HEADER: PREFER_RESPOND_ASYNC}
        return PredictOutcome(envelope=store.envelope(row), status_code=HTTPStatus.ACCEPTED, headers=headers)
    return PredictOutcome(envelope=store.envelope(row))


def _stored_failure(row) -> Problem:
    """The failure a sync call stored, raised again for a retry with the same key."""
    code = row["error"]["code"]
    return Problem(STORED_ERROR_STATUS[ProblemCode(code)], code, row["error"]["detail"])


async def _await_sync(ctx: KeyContext, row):
    """A sync prediction still running: wait for its handler up to predict.in_progress_wait_seconds; one whose handler died is failed."""
    deadline = time.monotonic() + settings.predict.in_progress_wait_seconds
    abandoned = PredictionError(code=ProblemCode.UPSTREAM_ERROR, detail=ABANDONED_DETAIL)
    while True:
        async with TenantConn(ctx.tenant_id) as conn:
            row = await store.abandon_if_expired(conn, row, abandoned) or await store.get(conn, row["id"])
        if row["status"] != PredictionStatus.IN_PROGRESS or time.monotonic() >= deadline:
            return row
        await asyncio.sleep(IN_PROGRESS_POLL_SECONDS)


async def _replay(ctx: KeyContext, row, hashed: str) -> PredictOutcome:
    """The prediction an earlier request with this Idempotency-Key made: no second Serve call, no second usage event."""
    if row["request_hash"] != hashed:
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.IDEMPOTENCY_KEY_REUSE, IDEMPOTENCY_REUSE_DETAIL)
    if row["mode"] == PredictionMode.SYNC:
        if row["status"] == PredictionStatus.IN_PROGRESS:
            row = await _await_sync(ctx, row)
        if row["status"] == PredictionStatus.IN_PROGRESS:
            busy = Problem(HTTPStatus.CONFLICT, ProblemCode.REQUEST_IN_PROGRESS, IN_PROGRESS_DETAIL)
            busy.headers = {RETRY_AFTER_HEADER: str(IN_PROGRESS_RETRY_AFTER_SECONDS)}
            raise busy
        if row["status"] == PredictionStatus.FAILED:
            raise _stored_failure(row)
    return _outcome(row)


async def _sync_stored(ctx: KeyContext, d, model_uuid: uuid.UUID, request: PredictRequest, key: str, hashed: str, size: int, max_inflight: int) -> PredictOutcome:
    """A sync call with an Idempotency-Key: the first one runs and is stored, a repeat gets what was stored."""
    async with TenantConn(ctx.tenant_id) as conn:
        row, created = await store.begin(
            conn, ctx, d["id"], model_uuid, d["mlflow_version"], request, mode=PredictionMode.SYNC, key=key, request_hash=hashed)
    if not created:
        return await _replay(ctx, row, hashed)
    try:
        output, elapsed_ms = await serve_call(ctx.tenant_id, ctx.project_id, d, request.input, size, max_inflight)
    except Problem as p:
        stored = p.code in STORED_ERROR_STATUS  # only a call that reached Serve is replayed; any other failure frees the key
        async with TenantConn(ctx.tenant_id) as conn:
            await store.finish(conn, row, PredictionStatus.FAILED, error=PredictionError(code=p.code, detail=p.detail), release_key=not stored)
        raise
    async with TenantConn(ctx.tenant_id) as conn:
        moved = await store.finish(conn, row, PredictionStatus.COMPLETED, output=output, latency_ms=elapsed_ms)
        if moved is None:  # the lease ran out under us and a retry ended the row: answer what it says
            moved = await store.get(conn, row["id"])
    return _outcome(moved)


async def _accept_background(ctx: KeyContext, d, model_uuid: uuid.UUID, request: PredictRequest, key: str, hashed: str) -> PredictOutcome:
    """`Prefer: respond-async`: store a queued prediction for the worker and answer 202; a repeat of the key answers what it made."""
    async with TenantConn(ctx.tenant_id) as conn:
        row, created = await store.begin(
            conn, ctx, d["id"], model_uuid, d["mlflow_version"], request, mode=PredictionMode.BACKGROUND, key=key, request_hash=hashed)
    return _outcome(row) if created else await _replay(ctx, row, hashed)


async def predict(
    ctx: KeyContext, model_id: str, revision: str | None, request: PredictRequest, *, idempotency_key: str | None = None, background: bool = False,
) -> PredictOutcome:
    if background and idempotency_key is None:
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, KernelProblemCode.VALIDATION_ERROR, IDEMPOTENCY_KEY_REQUIRED_DETAIL)
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
    await _rate_limited(row["id"], row["rate_limit_per_minute"])

    if idempotency_key is not None:
        hashed = request_hash(request, model_uuid, revision)
        if background:
            return await _accept_background(ctx, row, model_uuid, request, idempotency_key, hashed)
        return await _sync_stored(ctx, row, model_uuid, request, idempotency_key, hashed, size, limits.max_inflight_per_deployment)

    prediction_id = uuid.uuid4()
    output, elapsed_ms = await serve_call(ctx.tenant_id, ctx.project_id, row, request.input, size, limits.max_inflight_per_deployment)
    async with TenantConn(ctx.tenant_id) as conn:  # only a 2xx is metered, once per prediction id
        await record_usage(
            conn, tenant_id=ctx.tenant_id, project_id=ctx.project_id, resource_type=UsageResource.INFERENCE, quantity=1,
            unit=UsageUnit.REQUESTS, occurred_at=datetime.now(timezone.utc), source_ref=str(prediction_id))
    return PredictOutcome(envelope=PredictionEnvelope(
        id=f"{PREDICTION_ID_PREFIX}{prediction_id}", model=PredictModelRef(id=model_uuid, revision=row["mlflow_version"]), output=output,
        usage=PredictUsage(latency_ms=elapsed_ms), metadata=request.metadata, created_at=datetime.now(timezone.utc)))
