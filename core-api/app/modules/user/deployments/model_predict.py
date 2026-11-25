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

from app.core import plan_limits, rate_limit
from app.core.authz import CallSource, KeyContext
from app.core.metrics import inference as inference_metrics
from app.core.problem import Problem, ProblemCode as KernelProblemCode, not_found
from app.core.settings import settings
from app.core.tenant_conn import TenantConn
from app.integrations import serve_client
from app.integrations import data_plane
from app.modules.user.deployments import calls, predict as predict_path, prediction_store as store, service
from app.modules.user.deployments.constants import (
    ABANDONED_DETAIL,
    IDEMPOTENCY_KEY_REQUIRED_DETAIL,
    JSON_ACCEPTABLE,
    IDEMPOTENCY_REUSE_DETAIL,
    COLD_START_RETRY_AFTER_SECONDS,
    IN_PROGRESS_DETAIL,
    IN_PROGRESS_POLL_SECONDS,
    IN_PROGRESS_RETRY_AFTER_SECONDS,
    KEY_RATE_LIMIT_KEY,
    LOCATION_HEADER,
    MODEL_REJECTED_DETAIL_MAX_CHARS,
    NOT_STREAMABLE_DETAIL,
    PREDICT_PATH,
    PREDICTION_ID_PREFIX,
    PLAN_RATE_LIMIT_KEY,
    PREFER_RESPOND_ASYNC,
    PREFERENCE_APPLIED_HEADER,
    RATE_LIMIT_WINDOW_SECONDS,
    RETRY_AFTER_HEADER,
    SSE_MEDIA_TYPE,
    STORED_ERROR_STATUS,
    TERMINAL_STATUSES,
    UPSTREAM_ERROR_DETAIL,
    UPSTREAM_TIMEOUT_DETAIL,
    PredictionMode,
    PredictionStatus,
    ProblemCode,
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
from app.modules.user.tenants import get_plan_tier
from app.modules.user.usage import UsageResource, UsageUnit, record_usage

logger = logging.getLogger(__name__)

# The deployment a call is answered by: the one X-Model-Id names (a deployment id), or the project's main one when no id is sent.
# Its model and version come with it, for the stored prediction. The key's model allowlist was checked by require_key.
_RESOLVE = (
    "SELECT d.id, d.status, d.training_run_id, d.rate_limit_per_minute, mv.model_id, mv.mlflow_version "
    "FROM deployments d JOIN model_versions mv ON mv.id = d.model_version_id "
    "WHERE d.id = $1 AND d.project_id = $2 AND d.status <> $3"
)
_RESOLVE_MAIN = (
    "SELECT d.id, d.status, d.training_run_id, d.rate_limit_per_minute, mv.model_id, mv.mlflow_version "
    "FROM deployments d JOIN model_versions mv ON mv.id = d.model_version_id "
    "WHERE d.project_id = $1 AND d.main AND d.status <> $2"
)


def refuse_stream(accept: str | None) -> None:
    """`Accept: text/event-stream` and nothing that takes JSON: 406 until a serve adapter declares streaming (planning/24, Q4). The answer does not depend on the model, so it needs no lookup."""
    wanted = {part.split(";")[0].strip().lower() for part in (accept or "").split(",")}
    if SSE_MEDIA_TYPE in wanted and not wanted.intersection(JSON_ACCEPTABLE):
        raise Problem(HTTPStatus.NOT_ACCEPTABLE, ProblemCode.NOT_ACCEPTABLE, NOT_STREAMABLE_DETAIL)


def serve_problem(e: predict_path.ServeFailed) -> Problem:
    """Serve's own words are logged, never returned (except the model's refusal of the input, cut short)."""
    if e.code == serve_client.ServeErrorCode.BAD_INPUT:
        return Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.MODEL_REJECTED_INPUT, str(e.detail)[:MODEL_REJECTED_DETAIL_MAX_CHARS])
    logger.warning("serve failed (%s): %s", e.code, e.detail)
    if e.code == serve_client.ServeErrorCode.TIMEOUT:
        problem = Problem(HTTPStatus.GATEWAY_TIMEOUT, ProblemCode.UPSTREAM_TIMEOUT, UPSTREAM_TIMEOUT_DETAIL)
        problem.headers = {RETRY_AFTER_HEADER: str(COLD_START_RETRY_AFTER_SECONDS)}
        return problem
    return Problem(HTTPStatus.BAD_GATEWAY, ProblemCode.UPSTREAM_ERROR, UPSTREAM_ERROR_DETAIL)


async def _rate_limited(key: str, limit: int, subject: str) -> None:
    """429 with the seconds left in the current fixed one-minute window (when the count starts again) once `key` is over `limit`."""
    if not await rate_limit.check(key, limit):
        problem = Problem(HTTPStatus.TOO_MANY_REQUESTS, ProblemCode.RATE_LIMITED, f"rate limit exceeded ({limit}/min for {subject})")
        problem.headers = {RETRY_AFTER_HEADER: str(RATE_LIMIT_WINDOW_SECONDS - int(time.time()) % RATE_LIMIT_WINDOW_SECONDS)}
        raise problem


async def serve_call(tenant_id: str, project_id: uuid.UUID, d, input_, size: int, max_inflight: int) -> tuple[object, float]:
    """(output, milliseconds) of one call to the deployment `d` (id, training_run_id); a Problem when it fails.
    A failure that reached Serve has one of STORED_ERROR_STATUS's codes; anything else (no servable model, too many in flight) did not."""
    deployment_id, run_id = d["id"], d["training_run_id"]
    try:
        done = await predict_path.call(tenant_id, project_id, deployment_id, run_id, input_, size, max_inflight)
    except predict_path.ServeFailed as e:
        mapped = serve_problem(e)
        await calls.record(tenant_id, deployment_id, run_id, status=mapped.status_code, latency_ms=e.elapsed_ms, cold_load=None, error_code=e.code, request_bytes=size)
        raise mapped from e
    await calls.record(tenant_id, deployment_id, run_id, status=HTTPStatus.OK, latency_ms=done.elapsed_ms, cold_load=done.prediction.cold_load, error_code=None, request_bytes=size)
    inference_metrics.latency.record(done.elapsed_ms, {"deployment_id": str(deployment_id), "cold_load": done.prediction.cold_load})
    return {"predictions": done.prediction.predictions}, done.elapsed_ms


def request_hash(request: PredictRequest, deployment_id: uuid.UUID) -> str:
    """What makes two requests "the same" for an Idempotency-Key: input, metadata and the model the call named, as canonical JSON."""
    canonical = json.dumps(
        {"input": request.input, "metadata": request.metadata, "model": str(deployment_id)}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _outcome(row) -> PredictOutcome:
    """The answer for a stored prediction: a background one that is not finished is 202 with its Location, anything else 200."""
    if row["mode"] == PredictionMode.BACKGROUND and row["status"] not in TERMINAL_STATUSES:
        headers = {LOCATION_HEADER: f"{PREDICT_PATH}/{PREDICTION_ID_PREFIX}{row['id']}", PREFERENCE_APPLIED_HEADER: PREFER_RESPOND_ASYNC}
        return PredictOutcome(envelope=await store.public(row), status_code=HTTPStatus.ACCEPTED, headers=headers)
    return PredictOutcome(envelope=await store.public(row))


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
    return await _outcome(row)


async def _begin(ctx: KeyContext, d, model_uuid: uuid.UUID, request: PredictRequest, *, mode: PredictionMode, key: str, hashed: str):
    """store.begin with an input over predict.inline_max_bytes written to the bucket first (outside the transaction); the object of a request
    that turned out to be a repeat of the key is deleted again."""
    prediction_id, stored_input = await store.stash_input(ctx, request.input)
    async with TenantConn(ctx.tenant_id) as conn:
        row, created = await store.begin(
            conn, ctx, d["id"], model_uuid, d["mlflow_version"], request, mode=mode, key=key, request_hash=hashed,
            prediction_id=prediction_id, stored_input=stored_input)
    if not created and stored_input.ref is not None:
        await data_plane.delete_prediction_payloads(ctx.tenant_id, ctx.project_id, prediction_id)
    return row, created


async def _sync_stored(ctx: KeyContext, d, model_uuid: uuid.UUID, request: PredictRequest, key: str | None, hashed: str | None, size: int, max_inflight: int) -> PredictOutcome:
    """A sync call that is stored: with an Idempotency-Key the first one runs and a repeat gets what was stored; a console call has no key."""
    row, created = await _begin(ctx, d, model_uuid, request, mode=PredictionMode.SYNC, key=key, hashed=hashed)
    if not created:
        return await _replay(ctx, row, hashed)
    try:
        output, elapsed_ms = await serve_call(ctx.tenant_id, ctx.project_id, d, request.input, size, max_inflight)
    except Problem as p:
        stored = p.code in STORED_ERROR_STATUS  # only a call that reached Serve is replayed; any other failure frees the key
        async with TenantConn(ctx.tenant_id) as conn:
            await store.finish(conn, row, PredictionStatus.FAILED, error=PredictionError(code=p.code, detail=p.detail), release_key=not stored)
        raise
    stored_output = await store.stash_output(row, output)
    async with TenantConn(ctx.tenant_id) as conn:
        moved = await store.finish(
            conn, row, PredictionStatus.COMPLETED, output=stored_output.inline, output_ref=stored_output.ref, latency_ms=elapsed_ms)
        if moved is None:  # the lease ran out under us and a retry ended the row: answer what it says
            moved = await store.get(conn, row["id"])
    return await _outcome(moved)


async def _accept_background(ctx: KeyContext, d, model_uuid: uuid.UUID, request: PredictRequest, key: str, hashed: str) -> PredictOutcome:
    """`Prefer: respond-async`: store a queued prediction for the worker and answer 202; a repeat of the key answers what it made."""
    row, created = await _begin(ctx, d, model_uuid, request, mode=PredictionMode.BACKGROUND, key=key, hashed=hashed)
    return await _outcome(row) if created else await _replay(ctx, row, hashed)


async def predict(
    ctx: KeyContext, model_id: str | None, request: PredictRequest, *, idempotency_key: str | None = None, background: bool = False,
) -> PredictOutcome:
    if background and idempotency_key is None:
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, KernelProblemCode.VALIDATION_ERROR, IDEMPOTENCY_KEY_REQUIRED_DETAIL)
    try:
        deployment_uuid = None if model_id is None else uuid.UUID(model_id)
    except ValueError:
        raise not_found() from None
    async with TenantConn(ctx.tenant_id) as conn:
        if deployment_uuid is None:
            row = await conn.fetchrow(_RESOLVE_MAIN, ctx.project_id, DeploymentStatus.DELETED)
        else:
            row = await conn.fetchrow(_RESOLVE, deployment_uuid, ctx.project_id, DeploymentStatus.DELETED)
        limits = await plan_limits.predict_limits(conn, ctx.tenant_id)
        plan_limit = service.rate_limit_for(await get_plan_tier(conn, uuid.UUID(ctx.tenant_id)))
    if row is None:  # no such deployment, another project's, or no main one: one body
        raise not_found()
    # The key's allowlist names models (the project's registered model): the deployment's model must be on it.
    if not ctx.allows_model(str(row["model_id"])):
        raise Problem(HTTPStatus.FORBIDDEN, KernelProblemCode.FORBIDDEN, "this key may not use that model")
    model_uuid = row["model_id"]
    if row["status"] != DeploymentStatus.ACTIVE:  # deploying, stopped or failed: one code here
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.NOT_ACTIVE, f"the model's deployment is {row['status']}, not active")
    size = len(json.dumps(request.input))
    predict_path.check_size(size, limits)
    # The lower of the key's and the plan's limit: each counted on its own, the key first so a key over its limit does not use up the plan's.
    if ctx.rate_limit_per_minute is not None:
        await _rate_limited(KEY_RATE_LIMIT_KEY.format(ctx.key_id), ctx.rate_limit_per_minute, "this key")
    await _rate_limited(PLAN_RATE_LIMIT_KEY.format(ctx.tenant_id), plan_limit, "this plan")
    await _rate_limited(str(row["id"]), row["rate_limit_per_minute"], "this model")

    if idempotency_key is not None:
        hashed = request_hash(request, row["id"])
        if background:
            return await _accept_background(ctx, row, model_uuid, request, idempotency_key, hashed)
        return await _sync_stored(ctx, row, model_uuid, request, idempotency_key, hashed, size, limits.max_inflight_per_deployment)
    if ctx.source == CallSource.CONSOLE:  # the try-it log lists these: stored like a keyed call, with no key to replay
        return await _sync_stored(ctx, row, model_uuid, request, None, None, size, limits.max_inflight_per_deployment)

    prediction_id = uuid.uuid4()
    output, elapsed_ms = await serve_call(ctx.tenant_id, ctx.project_id, row, request.input, size, limits.max_inflight_per_deployment)
    async with TenantConn(ctx.tenant_id) as conn:  # only a 2xx is metered, once per prediction id
        await record_usage(
            conn, tenant_id=ctx.tenant_id, project_id=ctx.project_id, resource_type=UsageResource.INFERENCE, quantity=1,
            unit=UsageUnit.REQUESTS, occurred_at=datetime.now(timezone.utc), source_ref=str(prediction_id))
    return PredictOutcome(envelope=PredictionEnvelope(
        id=f"{PREDICTION_ID_PREFIX}{prediction_id}", model=PredictModelRef(id=row["id"]), output=output,
        usage=PredictUsage(latency_ms=elapsed_ms), metadata=request.metadata, created_at=datetime.now(timezone.utc)))
