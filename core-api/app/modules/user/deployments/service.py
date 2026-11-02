"""Deployment rules and queries: deploy, read, try-it and the key-authenticated predict path."""
import hashlib
import secrets
import time
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException

from app.core import rate_limit
from app.core import task_types
from app.core.authz import AuthzContext
from app.core.db import get_pool
from app.core.metrics import inference as inference_metrics, usage as usage_metrics
from app.core.tenant_conn import TenantConn
from app.integrations import serve_client
from app.modules.user.deployments.constants import (
    DEFAULT_COMPUTE_TARGET,
    MAIN_ROUTE_SUFFIX,
    METADATA_FIELD,
    NOT_DEPLOYABLE_DETAIL,
    PLAN_TIER_RATE_LIMITS,
    PREDICT_KEY_BYTES,
    SERVE_TIMEOUT_SECONDS,
    SETTING_COMPUTE_TARGET,
    SETTING_DEPLOY_STRATEGY,
    DeployStrategy,
    PlanTier,
)
from app.modules.user.deployments.schemas import (
    DeploymentCreated,
    DeploymentDetail,
    DeploymentListItem,
    TestCall,
    TestCallResult,
)
from app.modules.user.deployments.status import DeploymentStatus
from app.modules.user.projects import get_settings, get_task_type
from app.modules.user.tenants import get_plan_tier
from app.modules.user.training import get_run_model
from app.modules.user.usage import UsageResource, UsageUnit, record_usage

_NOT_FOUND = "deployment not found"


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def rate_limit_for(plan_tier: str | None) -> int:
    return PLAN_TIER_RATE_LIMITS.get(plan_tier, PLAN_TIER_RATE_LIMITS[PlanTier.FREE])


async def create_deployment(ctx: AuthzContext, project_id: uuid.UUID, training_run_id: uuid.UUID) -> DeploymentCreated:
    # The Serve deploy is slow and external: read what it needs, release the connection, deploy, then write.
    async with TenantConn(ctx.tenant_id) as conn:
        run = await get_run_model(conn, project_id, training_run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="training run not found")
        task = await get_task_type(conn, project_id)
        info = None if task is None else await task_types.get(conn, task)
        if info is None or not info.enabled or not info.servable or not run.servable:
            raise HTTPException(status_code=409, detail=NOT_DEPLOYABLE_DETAIL)

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
                "SELECT id, predict_key_hash, rate_limit_per_minute FROM deployments "
                "WHERE project_id = $1 AND route_prefix = $2",
                project_id,
                f"/predict/{project_id}/{MAIN_ROUTE_SUFFIX}",
            )
        plan_tier = await get_plan_tier(conn, ctx.tenant_id)

    deployment_id = uuid.UUID(str(existing_main["id"])) if existing_main else uuid.uuid4()
    ray_job_id, route_prefix = await serve_client.deploy(
        deployment_id=str(deployment_id),
        task=task,
        mlflow_run_id=run.mlflow_run_id,
        class_names=run.class_names,
        tenant_id=ctx.tenant_id,
        project_id=str(project_id),
        compute_target=serve_client.ComputeTarget(settings.get(SETTING_COMPUTE_TARGET, DEFAULT_COMPUTE_TARGET)),
        deploy_strategy=deploy_strategy,
    )

    async with TenantConn(ctx.tenant_id) as conn:
        if existing_main:
            row = await conn.fetchrow(
                "UPDATE deployments SET training_run_id = $2, ray_job_id = $3, status = $4 "
                "WHERE id = $1 RETURNING id, status, route_prefix, rate_limit_per_minute",
                deployment_id,
                training_run_id,
                ray_job_id,
                DeploymentStatus.DEPLOYING,
            )
            return DeploymentCreated.model_validate({**dict(row), "plan_tier": plan_tier})

        # Shown once — this is the credential a customer's downstream
        # system uses to call /predict, separate from the tenant/JWT
        # scope used everywhere else. Only the hash is stored.
        predict_key = secrets.token_urlsafe(PREDICT_KEY_BYTES)
        row = await conn.fetchrow(
            "INSERT INTO deployments (id, tenant_id, project_id, training_run_id, ray_job_id, route_prefix, predict_key_hash, rate_limit_per_minute) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id, status, route_prefix, rate_limit_per_minute",
            deployment_id,
            ctx.tenant_id,
            project_id,
            training_run_id,
            ray_job_id,
            route_prefix,
            hash_key(predict_key),
            rate_limit_for(plan_tier),
        )
    usage_metrics.deployments_created.add(1)
    return DeploymentCreated.model_validate({**dict(row), "predict_key": predict_key, "plan_tier": plan_tier})


async def list_deployments(ctx: AuthzContext, project_id: uuid.UUID) -> list[DeploymentListItem]:
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT id, status, route_prefix, created_at FROM deployments "
            "WHERE project_id = $1 ORDER BY created_at DESC",
            project_id,
        )
    return [DeploymentListItem.model_validate(dict(r)) for r in rows]


async def get_deployment(ctx: AuthzContext, project_id: uuid.UUID, deployment_id: uuid.UUID) -> DeploymentDetail:
    async with TenantConn(ctx.tenant_id) as conn:
        # Explicit columns: never predict_key_hash / ray_job_id. Read-only: the
        # deploying -> active/failed sync is the background reconciler's job.
        row = await conn.fetchrow(
            "SELECT id, project_id, training_run_id, status, route_prefix, rate_limit_per_minute, created_at "
            "FROM deployments WHERE id = $1 AND project_id = $2",
            deployment_id,
            project_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return DeploymentDetail.model_validate(dict(row))


async def _post_to_serve(route_prefix: str, body: dict) -> tuple[httpx.Response, float]:
    start = time.perf_counter()
    async with httpx.AsyncClient(timeout=SERVE_TIMEOUT_SECONDS) as client:
        resp = await client.post(f"{serve_client.serve_url()}{route_prefix}", json=body)
    return resp, (time.perf_counter() - start) * 1000


async def test_deployment(ctx: AuthzContext, project_id: uuid.UUID, deployment_id: uuid.UUID, body: dict) -> TestCallResult:
    # FR7 (06-project-settings-deployment.md): same auth as the management
    # API, for the in-app "try it" panel — no predict key needed. Logs to
    # deployment_test_calls so the UI can show a running log, not just the
    # latest call. The connection is released before the Serve call so a slow
    # model does not hold a pooled connection.
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT status, route_prefix FROM deployments WHERE id = $1 AND project_id = $2",
            deployment_id,
            project_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    if row["status"] != DeploymentStatus.ACTIVE:
        raise HTTPException(status_code=409, detail=f"deployment is {row['status']}, not active")

    resp, elapsed_ms = await _post_to_serve(row["route_prefix"], body)
    resp.raise_for_status()
    response_body = resp.json()

    async with TenantConn(ctx.tenant_id) as conn:
        await conn.execute(
            "INSERT INTO deployment_test_calls (tenant_id, deployment_id, request, response, latency_ms) "
            "VALUES ($1, $2, $3, $4, $5)",
            ctx.tenant_id,
            deployment_id,
            body,
            response_body,
            elapsed_ms,
        )
    return TestCallResult(response=response_body, latency_ms=elapsed_ms)


async def list_test_calls(ctx: AuthzContext, project_id: uuid.UUID, deployment_id: uuid.UUID, limit: int) -> list[TestCall]:
    async with TenantConn(ctx.tenant_id) as conn:
        owned = await conn.fetchval(
            "SELECT 1 FROM deployments WHERE id = $1 AND project_id = $2", deployment_id, project_id
        )
        if not owned:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        rows = await conn.fetch(
            "SELECT id, request, response, latency_ms, created_at FROM deployment_test_calls "
            "WHERE deployment_id = $1 ORDER BY created_at DESC LIMIT $2",
            deployment_id,
            limit,
        )
    return [TestCall.model_validate(dict(r)) for r in rows]


async def predict(deployment_id: uuid.UUID, predict_key: str, body: dict) -> dict:
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
        raise HTTPException(status_code=401, detail="invalid deployment id or predict key")
    if row["status"] != DeploymentStatus.ACTIVE:
        raise HTTPException(status_code=409, detail=f"deployment is {row['status']}, not active")
    if not await rate_limit.check(str(deployment_id), row["rate_limit_per_minute"]):
        raise HTTPException(
            status_code=429, detail=f"rate limit exceeded ({row['rate_limit_per_minute']}/min for this deployment)"
        )

    # FR6: an optional `metadata` passthrough field, popped before
    # forwarding to Serve (the model was never trained to expect it) and
    # echoed back alongside the prediction.
    metadata = body.pop(METADATA_FIELD, None)

    resp, elapsed_ms = await _post_to_serve(row["route_prefix"], body)
    inference_metrics.latency.record(elapsed_ms, {"deployment_id": str(deployment_id)})
    resp.raise_for_status()

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

    result = resp.json()
    if metadata is not None:
        result[METADATA_FIELD] = metadata
    return result
