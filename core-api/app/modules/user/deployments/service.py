"""Deployment rules and queries: deploy, read, try-it and the key-authenticated predict path."""
import hashlib
import json
import logging
import secrets
import time
import uuid
from datetime import datetime, timezone
from http import HTTPStatus

import asyncpg
import httpx

from app.core import audit, plan_limits, rate_limit
from app.core import task_types
from app.core.authz import AuthzContext, TenantStatus
from app.core.db import get_pool
from app.core.idempotency import insert_once, replay
from app.core.metrics import inference as inference_metrics, usage as usage_metrics
from app.core.problem import Problem, ProblemCode as KernelProblemCode, not_found
from app.core.tenant_conn import TenantConn
from app.integrations import job_lifecycle, serve_client
from app.modules.user.deployments import calls, shared, snippets
from app.modules.user.deployments.tasks import DEPLOYMENTS
from app.modules.user.deployments.constants import (
    INPUT_FIELD_BY_ENCODER,
    OUTPUT_ITEM_BY_POSTPROCESS,
    SNIPPET_INPUT_PLACEHOLDER,
    DEFAULT_COMPUTE_TARGET,
    DEPLOY_BELOW_FLOOR_DETAIL,
    DEPLOY_RISK_DETAIL,
    METRIC_FLOORS,
    DEPLOYMENT_EXISTS_DETAIL,
    DEPLOYMENT_LOCK_PREFIX,
    AuditAction,
    EntityType,
    MAIN_ROUTE_SUFFIX,
    METADATA_FIELD,
    PREDICTIONS_FIELD,
    NOT_DEPLOYABLE_DETAIL,
    ProblemCode,
    PLAN_TIER_RATE_LIMITS,
    PREDICT_KEY_BYTES,
    RATE_LIMIT_WINDOW_SECONDS,
    RETRY_AFTER_HEADER,
    SERVE_TIMEOUT_SECONDS,
    STOPPED_DETAIL,
    TOO_MANY_DEPLOYMENTS_DETAIL,
    SETTING_COMPUTE_TARGET,
    SETTING_DEPLOY_STRATEGY,
    TEST_CALL_STORED_MAX_BYTES,
    TRUNCATED_BYTES_FIELD,
    TRUNCATED_FIELD,
    DeployStrategy,
    PlanTier,
    Serving,
)
from app.modules.user.deployments.schemas import (
    DeploymentSchema,
    DeploymentCreated,
    DeploymentDetail,
    DeploymentListItem,
    TestCall,
    TestCallResult,
)
from app.modules.user.deployments.status import QUOTA_STATUSES, DeploymentStatus
from app.modules.user.projects import get_settings, get_task_type
from app.modules.user.tenants import get_plan_tier
from app.modules.user.training import get_run_model
from app.modules.user.usage import UsageResource, UsageUnit, record_usage


logger = logging.getLogger(__name__)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def rate_limit_for(plan_tier: str | None) -> int:
    return PLAN_TIER_RATE_LIMITS.get(plan_tier, PLAN_TIER_RATE_LIMITS[PlanTier.FREE])


def _check_size(size: int, limits: plan_limits.Predict) -> None:
    """The plan's body cap, which is at most the ceiling the route already refused above before parsing."""
    if size > limits.max_body_bytes:
        raise Problem(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, ProblemCode.BODY_TOO_LARGE, f"the body is over {limits.max_body_bytes} bytes (this plan's limit)")


async def _deploy_legacy(ctx, project_id, deployment_id, run, info, settings, deploy_strategy) -> tuple[str, str]:
    """Start the deployment's own Serve app: (ray_job_id, route_prefix)."""
    return await serve_client.deploy(
        deployment_id=str(deployment_id),
        task=info,
        mlflow_run_id=run.mlflow_run_id,
        class_names=run.class_names,
        tenant_id=ctx.tenant_id,
        project_id=str(project_id),
        compute_target=serve_client.ComputeTarget(settings.get(SETTING_COMPUTE_TARGET, DEFAULT_COMPUTE_TARGET)),
        deploy_strategy=deploy_strategy,
    )


_LOCK_TENANT = "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))"
_ACTIVE_COUNT = "SELECT count(*) FROM deployments WHERE tenant_id = $1 AND id <> $2 AND status = ANY($3::text[])"
_BY_KEY = "SELECT * FROM deployments WHERE project_id = $1 AND idempotency_key = $2"
# The model version a deployment serves is the newest registered from its run (versions are 1:1 with runs today); NULL when none is.
_VERSION_OF_RUN = "(SELECT id FROM model_versions WHERE training_run_id = {run} ORDER BY created_at DESC, id LIMIT 1)"
_INSERT = (
    "INSERT INTO deployments (id, tenant_id, project_id, training_run_id, ray_job_id, route_prefix, predict_key_hash, "
    "rate_limit_per_minute, serving, status, idempotency_key, risk_acknowledged, model_version_id) "
    f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, {_VERSION_OF_RUN.format(run='$4')}) "
    "ON CONFLICT (project_id, idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING "
    "RETURNING id, status, route_prefix, rate_limit_per_minute, training_run_id"
)
_QUOTA_STATUSES = [str(s) for s in QUOTA_STATUSES]


async def _check_quota(conn, tenant_id: str, deployment_id: uuid.UUID, *, lock: bool = False) -> None:
    """429 when the tenant's other active or deploying deployments already fill the plan's slots. `lock` serialises tenants'
    writes on an advisory lock first, so two concurrent creates cannot both take the last slot."""
    if lock:
        await conn.fetchval(_LOCK_TENANT, f"{DEPLOYMENT_LOCK_PREFIX}{tenant_id}")
    if await conn.fetchval(_ACTIVE_COUNT, tenant_id, deployment_id, _QUOTA_STATUSES) >= await plan_limits.max_active_deployments(conn, tenant_id):
        raise Problem(HTTPStatus.TOO_MANY_REQUESTS, ProblemCode.TOO_MANY_DEPLOYMENTS, TOO_MANY_DEPLOYMENTS_DETAIL)


def _replay(row, training_run_id: uuid.UUID) -> DeploymentCreated:
    """The deployment an earlier request with this key made. Only a hash of its predict key is stored, so it is not shown again."""
    same = replay(row, lambda r: r["training_run_id"] == training_run_id, ProblemCode.IDEMPOTENCY_MISMATCH)
    return DeploymentCreated.model_validate({**dict(same), "plan_tier": None})


async def _drop_stray_app(ctx: AuthzContext, project_id: uuid.UUID, deployment_id: uuid.UUID, serving: Serving, strategy: DeployStrategy) -> None:
    """A create that lost a race after starting its own legacy Serve app removes it (a `main` app is shared by the winner, so it stays)."""
    if serving == Serving.LEGACY and strategy == DeployStrategy.LATEST:
        try:
            await serve_client.remove(deployment_id=str(deployment_id), project_id=str(project_id), tenant_id=ctx.tenant_id, deploy_strategy=strategy)
        except Exception:  # noqa: BLE001 - best effort, the app is unreferenced either way
            logger.warning("could not remove the Serve app of deployment %s", deployment_id)


async def create_deployment(
    ctx: AuthzContext, project_id: uuid.UUID, training_run_id: uuid.UUID, idempotency_key: str, acknowledge_risk: bool = False,
) -> DeploymentCreated:
    # The Serve deploy is slow and external: read what it needs, release the connection, deploy, then write.
    async with TenantConn(ctx.tenant_id) as conn:
        existing = await conn.fetchrow(_BY_KEY, project_id, idempotency_key)
        if existing is not None:
            return _replay(existing, training_run_id)
        run = await get_run_model(conn, project_id, training_run_id)
        if run is None:
            raise not_found()
        task = await get_task_type(conn, project_id)
        info = None if task is None else await task_types.get(conn, task)
        if info is None or not info.enabled or not info.servable or not run.servable:
            raise Problem(HTTPStatus.CONFLICT, ProblemCode.NOT_DEPLOYABLE, NOT_DEPLOYABLE_DETAIL)
        risk = None if run.has_test_split else DEPLOY_RISK_DETAIL
        metric, floor = METRIC_FLOORS.get(task, (None, 0.0))
        score = (run.metrics or {}).get(metric)
        if risk is None and isinstance(score, int | float) and score < floor:
            risk = DEPLOY_BELOW_FLOOR_DETAIL.format(metric=metric, value=score, floor=floor)
        risky = risk is not None
        if risky and not acknowledge_risk:  # a warning with an acknowledgement, not a block (planning/06 Quality gates)
            raise Problem(HTTPStatus.CONFLICT, ProblemCode.DEPLOY_RISK, risk)

        settings = await get_settings(conn, project_id)
        deploy_strategy = settings.get(SETTING_DEPLOY_STRATEGY, DeployStrategy.LATEST)

        # FR4/FR5 (06-project-settings-deployment.md): 'main' strategy has
        # at most one deployment row per project — a re-deploy updates it
        # in place (same route, same predict key) instead of minting a new
        # one, so "promote this version to main" never breaks a downstream
        # integrator's URL.
        existing_main = None
        if deploy_strategy == DeployStrategy.MAIN:
            existing_main = await conn.fetchrow(
                "SELECT id, status, predict_key_hash, rate_limit_per_minute FROM deployments "
                "WHERE project_id = $1 AND route_prefix = $2 AND status <> $3",
                project_id,
                f"/predict/{project_id}/{MAIN_ROUTE_SUFFIX}",
                DeploymentStatus.DELETED,  # a deleted row has no key: the next deploy mints a new one
            )
            if existing_main and existing_main["status"] == DeploymentStatus.STOPPED:
                raise Problem(HTTPStatus.CONFLICT, ProblemCode.STOPPED, STOPPED_DETAIL)
        plan_tier = await get_plan_tier(conn, ctx.tenant_id)
        await _check_quota(conn, ctx.tenant_id, existing_main["id"] if existing_main else uuid.uuid4())  # cheap early answer; the one that counts is under the lock below

    deployment_id = uuid.UUID(str(existing_main["id"])) if existing_main else uuid.uuid4()
    if await serve_client.apps_declared():
        # The predictor apps load the model on its first request: nothing to start, so the deployment is active at once.
        ray_job_id, serving, status = None, Serving.SHARED, DeploymentStatus.ACTIVE
        route_prefix = serve_client.route_prefix_for(str(deployment_id), str(project_id), deploy_strategy)
    else:
        ray_job_id, route_prefix = await _deploy_legacy(ctx, project_id, deployment_id, run, info, settings, deploy_strategy)
        serving, status = Serving.LEGACY, DeploymentStatus.DEPLOYING

    try:
        return await _write_deployment(
            ctx, project_id, training_run_id, idempotency_key, deployment_id, existing_main is not None,
            ray_job_id, route_prefix, serving, status, plan_tier, risky)
    except Exception:  # ponytail: a create that loses an Idempotency-Key race returns normally and leaves its legacy app to the winner's reconcile
        if existing_main is None:
            await _drop_stray_app(ctx, project_id, deployment_id, serving, deploy_strategy)
        raise


async def _write_deployment(
    ctx: AuthzContext, project_id: uuid.UUID, training_run_id: uuid.UUID, idempotency_key: str, deployment_id: uuid.UUID,
    in_place: bool, ray_job_id, route_prefix, serving, status, plan_tier, risk_acknowledged: bool = False,
) -> DeploymentCreated:
    """The write half of a deploy, under the tenant's deployments lock. One live deployment per run is the unique index's rule:
    a violation is 409. A `main` re-deploy moves the one row to the new run (and takes the new key)."""
    try:
        async with TenantConn(ctx.tenant_id) as conn:
            await _check_quota(conn, ctx.tenant_id, deployment_id, lock=True)
            if in_place:
                row = await conn.fetchrow(
                    "UPDATE deployments SET training_run_id = $2, ray_job_id = $3, status = $4, serving = $5, idempotency_key = $7, "
                    f"risk_acknowledged = $8, model_version_id = {_VERSION_OF_RUN.format(run='$2')} "
                    "WHERE id = $1 AND status <> $6 RETURNING id, status, route_prefix, rate_limit_per_minute",
                    deployment_id,
                    training_run_id,
                    ray_job_id,
                    status,
                    serving,
                    DeploymentStatus.STOPPED,  # stopped since the read: a deploy never resurrects it
                    idempotency_key,
                    risk_acknowledged,
                )
                if row is None:
                    raise Problem(HTTPStatus.CONFLICT, ProblemCode.STOPPED, STOPPED_DETAIL)
                await audit.record_for(conn, ctx, project_id, AuditAction.REDEPLOY, EntityType.DEPLOYMENT, deployment_id)
                await job_lifecycle.emit(conn, DEPLOYMENTS, ctx.tenant_id, project_id, deployment_id, row["status"])
                return DeploymentCreated.model_validate({**dict(row), "plan_tier": plan_tier})

            # Shown once — this is the credential a customer's downstream
            # system uses to call /predict, separate from the tenant/JWT
            # scope used everywhere else. Only the hash is stored.
            predict_key = secrets.token_urlsafe(PREDICT_KEY_BYTES)
            row, created = await insert_once(
                conn, _INSERT,
                (deployment_id, ctx.tenant_id, project_id, training_run_id, ray_job_id, route_prefix, hash_key(predict_key),
                 rate_limit_for(plan_tier), serving, status, idempotency_key, risk_acknowledged),
                _BY_KEY, (project_id, idempotency_key),
            )
            if not created:  # lost a race with the same key: that one is the deployment
                return _replay(row, training_run_id)
            await audit.record_for(conn, ctx, project_id, AuditAction.CREATE, EntityType.DEPLOYMENT, deployment_id)
            await job_lifecycle.emit(conn, DEPLOYMENTS, ctx.tenant_id, project_id, deployment_id, row["status"])
    except asyncpg.UniqueViolationError:  # only a unique index on the version or the run is left (deployments_one_active_per_version, deployments_one_live_per_run): the idempotency key conflict is DO NOTHING above
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.DEPLOYMENT_EXISTS, DEPLOYMENT_EXISTS_DETAIL) from None
    usage_metrics.deployments_created.add(1)
    return DeploymentCreated.model_validate({**dict(row), "predict_key": predict_key, "plan_tier": plan_tier})


async def list_deployments(ctx: AuthzContext, project_id: uuid.UUID) -> list[DeploymentListItem]:
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT id, status, route_prefix, created_at FROM deployments "
            "WHERE project_id = $1 AND status <> $2 ORDER BY created_at DESC",
            project_id,
            DeploymentStatus.DELETED,
        )
    return [DeploymentListItem.model_validate(dict(r)) for r in rows]


async def get_deployment(ctx: AuthzContext, project_id: uuid.UUID, deployment_id: uuid.UUID) -> DeploymentDetail:
    async with TenantConn(ctx.tenant_id) as conn:
        # Explicit columns: never predict_key_hash / ray_job_id. Read-only: the
        # deploying -> active/failed sync is the background reconciler's job.
        row = await conn.fetchrow(
            "SELECT id, project_id, training_run_id, status, route_prefix, rate_limit_per_minute, created_at "
            "FROM deployments WHERE id = $1 AND project_id = $2 AND status <> $3",
            deployment_id,
            project_id,
            DeploymentStatus.DELETED,
        )
    if row is None:
        raise not_found()
    return DeploymentDetail.model_validate(dict(row))


async def get_schema(ctx: AuthzContext, project_id: uuid.UUID, deployment_id: uuid.UUID, base_url: str) -> DeploymentSchema:
    """The input and output shape for POST /v1/predict on this deployment, from its task type's encoder and postprocess, and call snippets."""
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT mv.model_id, mv.mlflow_version FROM deployments d JOIN model_versions mv ON mv.id = d.model_version_id "
            "WHERE d.id = $1 AND d.project_id = $2 AND d.status <> $3",
            deployment_id, project_id, DeploymentStatus.DELETED,
        )
        task = await get_task_type(conn, project_id)
        info = None if task is None else await task_types.get(conn, task)
    if row is None or info is None:
        raise not_found()
    field = INPUT_FIELD_BY_ENCODER.get(info.encoder)
    body_input = {field: SNIPPET_INPUT_PLACEHOLDER} if field else {}
    item = OUTPUT_ITEM_BY_POSTPROCESS.get(info.postprocess, {})
    return DeploymentSchema(
        task_type=task, model_id=row["model_id"], model_revision=row["mlflow_version"],
        input={field: "string"} if field else {}, output={"predictions": [item]},
        snippets=snippets.build(base_url, str(project_id), str(row["model_id"]), row["mlflow_version"], {"input": body_input}),
    )


def _require_active(status: str) -> None:
    """409 unless the deployment serves; a stopped one says so by its own code (its predict key stays valid)."""
    if status == DeploymentStatus.STOPPED:
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.STOPPED, STOPPED_DETAIL)
    if status != DeploymentStatus.ACTIVE:
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.NOT_ACTIVE, f"deployment is {status}, not active")


async def _post_to_serve(route_prefix: str, body: dict) -> tuple[httpx.Response, float]:
    start = time.perf_counter()
    async with httpx.AsyncClient(timeout=SERVE_TIMEOUT_SECONDS) as client:
        resp = await client.post(f"{serve_client.serve_url()}{route_prefix}", json=body)
    return resp, (time.perf_counter() - start) * 1000


def _stored(payload):
    """What the try-it log keeps of a request or response: itself, or a marker with its size when it is over the cap."""
    size = len(json.dumps(payload))
    return payload if size <= TEST_CALL_STORED_MAX_BYTES else {TRUNCATED_FIELD: True, TRUNCATED_BYTES_FIELD: size}


async def test_deployment(ctx: AuthzContext, project_id: uuid.UUID, deployment_id: uuid.UUID, body: dict) -> TestCallResult:
    # FR7 (06-project-settings-deployment.md): same auth as the management
    # API, for the in-app "try it" panel — no predict key needed. Logs to
    # deployment_test_calls so the UI can show a running log, not just the
    # latest call. The connection is released before the Serve call so a slow
    # model does not hold a pooled connection.
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT status, route_prefix, serving, training_run_id FROM deployments WHERE id = $1 AND project_id = $2 AND status <> $3",
            deployment_id,
            project_id,
            DeploymentStatus.DELETED,
        )
    if row is None:
        raise not_found()
    _require_active(row["status"])

    if row["serving"] == Serving.SHARED:
        async with TenantConn(ctx.tenant_id) as conn:
            limits = await plan_limits.predict_limits(conn, ctx.tenant_id)
        _check_size(len(json.dumps(body)), limits)
        prediction, elapsed_ms = await shared.answer(
            ctx.tenant_id, project_id, deployment_id, row["training_run_id"], body, limits.max_inflight_per_deployment)
        response_body = {PREDICTIONS_FIELD: prediction.predictions}
    else:
        resp, elapsed_ms = await _post_to_serve(row["route_prefix"], body)
        resp.raise_for_status()
        response_body = resp.json()

    async with TenantConn(ctx.tenant_id) as conn:
        await conn.execute(
            "INSERT INTO deployment_test_calls (tenant_id, deployment_id, request, response, latency_ms) "
            "VALUES ($1, $2, $3, $4, $5)",
            ctx.tenant_id,
            deployment_id,
            _stored(body),
            _stored(response_body),
            elapsed_ms,
        )
    return TestCallResult(response=response_body, latency_ms=elapsed_ms)


async def list_test_calls(ctx: AuthzContext, project_id: uuid.UUID, deployment_id: uuid.UUID, limit: int) -> list[TestCall]:
    async with TenantConn(ctx.tenant_id) as conn:
        owned = await conn.fetchval(
            "SELECT 1 FROM deployments WHERE id = $1 AND project_id = $2 AND status <> $3",
            deployment_id, project_id, DeploymentStatus.DELETED,
        )
        if not owned:
            raise not_found()
        rows = await conn.fetch(
            "SELECT id, request, response, latency_ms, created_at FROM deployment_test_calls "
            "WHERE deployment_id = $1 ORDER BY created_at DESC LIMIT $2",
            deployment_id,
            limit,
        )
    return [TestCall.model_validate(dict(r)) for r in rows]


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
    _require_active(row["status"])
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
    # FR6: an optional `metadata` passthrough field, popped before
    # forwarding to Serve (the model was never trained to expect it) and
    # echoed back alongside the prediction.
    metadata = body.pop(METADATA_FIELD, None)

    async with TenantConn(str(row["tenant_id"])) as conn:
        served = await conn.fetchrow("SELECT serving, training_run_id FROM deployments WHERE id = $1", deployment_id)
        limits = await plan_limits.predict_limits(conn, str(row["tenant_id"]))
    if served["serving"] == Serving.SHARED:
        tenant_id, run_id, size = str(row["tenant_id"]), served["training_run_id"], len(json.dumps(body))
        _check_size(size, limits)
        try:
            prediction, elapsed_ms = await shared.answer(tenant_id, row["project_id"], deployment_id, run_id, body, limits.max_inflight_per_deployment)
        except shared.ServeFailed as e:
            await calls.record(tenant_id, deployment_id, run_id, status=e.status_code, latency_ms=e.elapsed_ms, cold_load=None, error_code=e.code, request_bytes=size)
            raise
        inference_metrics.latency.record(elapsed_ms, {"deployment_id": str(deployment_id), "cold_load": prediction.cold_load})
        await calls.record(tenant_id, deployment_id, run_id, status=HTTPStatus.OK, latency_ms=elapsed_ms, cold_load=prediction.cold_load, error_code=None, request_bytes=size)
        result = {PREDICTIONS_FIELD: prediction.predictions}
    else:
        resp, elapsed_ms = await _post_to_serve(row["route_prefix"], body)
        inference_metrics.latency.record(elapsed_ms, {"deployment_id": str(deployment_id)})
        resp.raise_for_status()
        result = resp.json()

    # FR2 (09-usage-billing-two-tier.md): meter inference — request id in
    # source_ref (not just deployment_id) so the existing
    # UNIQUE(resource_type, source_ref) idempotency key doesn't collapse
    # distinct requests into one row.
    async with TenantConn(str(row["tenant_id"])) as usage_conn:
        await record_usage(
            usage_conn,
            tenant_id=str(row["tenant_id"]),
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


# ---- lifecycle: stop, start, delete ----

_STATE_COLUMNS = "id, project_id, training_run_id, status, route_prefix, rate_limit_per_minute, created_at, serving"


def _strategy_of(row) -> DeployStrategy:
    """A `main` deployment is the one on the project's stable route."""
    main = serve_client.route_prefix_for(str(row["id"]), str(row["project_id"]), DeployStrategy.MAIN)
    return DeployStrategy.MAIN if row["route_prefix"] == main else DeployStrategy.LATEST


async def _move(
    ctx: AuthzContext, project_id: uuid.UUID, deployment_id: uuid.UUID, *,
    frm: tuple[DeploymentStatus, ...], to: DeploymentStatus, action: AuditAction, ray_job_id: str | None = None,
) -> tuple[dict, bool]:
    """Compare-and-set the status from one of `frm` to `to`, audited in the same transaction. Returns (row, moved): a row
    already at `to` comes back with moved False (a repeat is not an error); any other status is 409, a missing or deleted row 404."""
    async with TenantConn(ctx.tenant_id) as conn:
        if to in QUOTA_STATUSES:  # starting takes a slot back: stopped freed it
            await _check_quota(conn, ctx.tenant_id, deployment_id, lock=True)
        row = await conn.fetchrow(
            "UPDATE deployments SET status = $3, ray_job_id = COALESCE($5, ray_job_id), "
            "predict_key_hash = CASE WHEN $6 THEN NULL ELSE predict_key_hash END "
            f"WHERE id = $1 AND project_id = $2 AND status = ANY($4::text[]) RETURNING {_STATE_COLUMNS}",  # noqa: S608
            deployment_id, project_id, to, [str(s) for s in frm], ray_job_id, to == DeploymentStatus.DELETED,
        )
        if row is not None:
            await audit.record_for(conn, ctx, project_id, action, EntityType.DEPLOYMENT, deployment_id)
            await job_lifecycle.emit(conn, DEPLOYMENTS, ctx.tenant_id, project_id, deployment_id, to)
            return dict(row), True
        row = await conn.fetchrow(
            f"SELECT {_STATE_COLUMNS} FROM deployments WHERE id = $1 AND project_id = $2 AND status <> $3",  # noqa: S608
            deployment_id, project_id, DeploymentStatus.DELETED,
        )
    if row is None:
        raise not_found()
    if row["status"] == to:
        return dict(row), False
    raise Problem(HTTPStatus.CONFLICT, ProblemCode.INVALID_TRANSITION, f"a {row['status']} deployment cannot {action}")


async def _remove_serve_app(ctx: AuthzContext, row: dict) -> None:
    if row["serving"] == Serving.LEGACY:  # a shared deployment has no Serve app of its own
        await serve_client.remove(
            deployment_id=str(row["id"]), project_id=str(row["project_id"]), tenant_id=ctx.tenant_id, deploy_strategy=_strategy_of(row))


async def stop_deployment(ctx: AuthzContext, project_id: uuid.UUID, deployment_id: uuid.UUID) -> DeploymentDetail:
    """active -> stopped. Predict and try-it answer 409 until it starts again; the predict key is kept. A repeat retries
    removing the Serve app, so a removal that failed after the status moved can be finished."""
    row, _ = await _move(ctx, project_id, deployment_id, frm=(DeploymentStatus.ACTIVE,), to=DeploymentStatus.STOPPED, action=AuditAction.STOP)
    await _remove_serve_app(ctx, row)
    return DeploymentDetail.model_validate(row)


async def delete_deployment(ctx: AuthzContext, project_id: uuid.UUID, deployment_id: uuid.UUID) -> None:
    """Soft delete from active, stopped or failed: the row stays for lineage, its predict key can never match again."""
    row, _ = await _move(
        ctx, project_id, deployment_id,
        frm=(DeploymentStatus.ACTIVE, DeploymentStatus.STOPPED, DeploymentStatus.FAILED), to=DeploymentStatus.DELETED, action=AuditAction.DELETE)
    await _remove_serve_app(ctx, row)


async def _restart_legacy(ctx: AuthzContext, project_id: uuid.UUID, row: dict) -> str:
    """Deploy the stopped deployment's Serve app again from its own run; returns the job id."""
    async with TenantConn(ctx.tenant_id) as conn:
        run = await get_run_model(conn, project_id, row["training_run_id"])
        task = await get_task_type(conn, project_id)
        info = None if task is None else await task_types.get(conn, task)
        settings = await get_settings(conn, project_id)
    if run is None or info is None or not info.enabled or not info.servable or not run.servable:
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.NOT_DEPLOYABLE, NOT_DEPLOYABLE_DETAIL)
    ray_job_id, _ = await _deploy_legacy(ctx, project_id, row["id"], run, info, settings, _strategy_of(row))
    return ray_job_id


async def start_deployment(ctx: AuthzContext, project_id: uuid.UUID, deployment_id: uuid.UUID) -> DeploymentDetail:
    """stopped -> active (shared) or deploying (legacy: redeployed, the reconciler makes it active). Same row, same key."""
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            f"SELECT {_STATE_COLUMNS} FROM deployments WHERE id = $1 AND project_id = $2 AND status <> $3",  # noqa: S608
            deployment_id, project_id, DeploymentStatus.DELETED,
        )
    if row is None:
        raise not_found()
    legacy = row["serving"] == Serving.LEGACY
    to = DeploymentStatus.DEPLOYING if legacy else DeploymentStatus.ACTIVE
    ray_job_id = await _restart_legacy(ctx, project_id, dict(row)) if legacy and row["status"] == DeploymentStatus.STOPPED else None
    moved, changed = await _move(ctx, project_id, deployment_id, frm=(DeploymentStatus.STOPPED,), to=to, action=AuditAction.START, ray_job_id=ray_job_id)
    if ray_job_id is not None and not changed:  # someone else started it first: this job is a duplicate
        await job_lifecycle.stop_quietly(ray_job_id)
    return DeploymentDetail.model_validate(moved)
