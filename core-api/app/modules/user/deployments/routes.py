import hashlib
import secrets
import time
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Body, Depends, Header, HTTPException

from app.core import metrics as app_metrics, rate_limit
from app.core.db import get_pool
from app.core.tenancy import get_tenant_id
from app.core.tenant_conn import TenantConn
from app.integrations import ray_client, serve_client
from app.modules.user.usage import record_usage

router = APIRouter(tags=["deployments"])

# req/04 FR4: rate limit "configurable per tenant plan tier."
PLAN_TIER_RATE_LIMITS = {"free": 10, "pro": 120, "enterprise": 1000}


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


@router.post("/v1/projects/{project_id}/deployments", status_code=202)
async def create_deployment(
    project_id: uuid.UUID, training_run_id: uuid.UUID, tenant_id: str = Depends(get_tenant_id)
):
    async with TenantConn(tenant_id) as conn:
        run = await conn.fetchrow(
            "SELECT status, model_artifact FROM training_runs WHERE id = $1 AND project_id = $2",
            training_run_id,
            project_id,
        )
        if run is None:
            raise HTTPException(status_code=404, detail="training run not found")
        if run["status"] != "succeeded" or run["model_artifact"] is None:
            raise HTTPException(
                status_code=409,
                detail="training run has no deployable model artifact "
                "(only the default numpy backend currently produces one — see README)",
            )

        settings = await conn.fetchval("SELECT settings FROM projects WHERE id = $1", project_id) or {}
        deploy_strategy = settings.get("deploy_strategy", "latest")

        # FR4/FR5 (06-project-settings-deployment.md): 'main' strategy has
        # at most one deployment row per project — a re-deploy updates it
        # in place (same route, same predict key) instead of minting a new
        # one, so "promote this version to main" never breaks a downstream
        # integrator's URL.
        existing_main = None
        if deploy_strategy == "main":
            existing_main = await conn.fetchrow(
                "SELECT id, predict_key_hash, rate_limit_per_minute FROM deployments "
                "WHERE project_id = $1 AND route_prefix = $2",
                project_id,
                f"/predict/{project_id}/main",
            )

        deployment_id = uuid.UUID(str(existing_main["id"])) if existing_main else uuid.uuid4()
        weights = run["model_artifact"]
        ray_job_id, route_prefix = await serve_client.deploy(
            deployment_id=str(deployment_id),
            weights=weights,
            tenant_id=tenant_id,
            project_id=str(project_id),
            compute_target=settings.get("compute_target", "cpu"),
            deploy_strategy=deploy_strategy,
        )

        plan_tier = await conn.fetchval("SELECT plan_tier FROM tenants WHERE id = $1", tenant_id)

        if existing_main:
            row = await conn.fetchrow(
                "UPDATE deployments SET training_run_id = $2, ray_job_id = $3, status = 'deploying' "
                "WHERE id = $1 RETURNING id, status, route_prefix, rate_limit_per_minute",
                deployment_id,
                training_run_id,
                ray_job_id,
            )
            return {**dict(row), "plan_tier": plan_tier}

        # Shown once — this is the credential a customer's downstream
        # system uses to call /predict, separate from the tenant/JWT
        # scope used everywhere else. Only the hash is stored.
        predict_key = secrets.token_urlsafe(32)
        rate_limit_default = PLAN_TIER_RATE_LIMITS.get(plan_tier, PLAN_TIER_RATE_LIMITS["free"])

        row = await conn.fetchrow(
            "INSERT INTO deployments (id, tenant_id, project_id, training_run_id, ray_job_id, route_prefix, predict_key_hash, rate_limit_per_minute) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id, status, route_prefix, rate_limit_per_minute",
            deployment_id,
            tenant_id,
            project_id,
            training_run_id,
            ray_job_id,
            route_prefix,
            _hash_key(predict_key),
            rate_limit_default,
        )
    return {**dict(row), "predict_key": predict_key, "plan_tier": plan_tier}


@router.get("/v1/projects/{project_id}/deployments")
async def list_deployments(project_id: uuid.UUID, tenant_id: str = Depends(get_tenant_id)):
    async with TenantConn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT id, status, route_prefix, created_at FROM deployments "
            "WHERE project_id = $1 ORDER BY created_at DESC",
            project_id,
        )
    return [dict(r) for r in rows]


@router.get("/v1/projects/{project_id}/deployments/{deployment_id}")
async def get_deployment(
    project_id: uuid.UUID, deployment_id: uuid.UUID, tenant_id: str = Depends(get_tenant_id)
):
    async with TenantConn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM deployments WHERE id = $1 AND project_id = $2", deployment_id, project_id
        )
        if row is None:
            raise HTTPException(status_code=404, detail="deployment not found")
        row = dict(row)
        if row["status"] == "deploying":
            ray_status = await ray_client.get_status(row["ray_job_id"])
            if ray_status == "SUCCEEDED":
                row = dict(
                    await conn.fetchrow(
                        "UPDATE deployments SET status = 'active' WHERE id = $1 RETURNING *", deployment_id
                    )
                )
            elif ray_status == "FAILED":
                row = dict(
                    await conn.fetchrow(
                        "UPDATE deployments SET status = 'failed' WHERE id = $1 RETURNING *", deployment_id
                    )
                )
    return row


@router.post("/v1/projects/{project_id}/deployments/{deployment_id}/test")
async def test_deployment(
    project_id: uuid.UUID,
    deployment_id: uuid.UUID,
    body: dict = Body(...),
    tenant_id: str = Depends(get_tenant_id),
):
    # FR7 (06-project-settings-deployment.md): same auth as the management
    # API, for the in-app "try it" panel — no predict key needed. Logs to
    # deployment_test_calls so the UI can show a running log, not just the
    # latest call.
    async with TenantConn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT status, route_prefix FROM deployments WHERE id = $1 AND project_id = $2",
            deployment_id,
            project_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="deployment not found")
        if row["status"] != "active":
            raise HTTPException(status_code=409, detail=f"deployment is {row['status']}, not active")

        start = time.perf_counter()
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{serve_client.RAY_SERVE_HTTP}{row['route_prefix']}", json=body)
        elapsed_ms = (time.perf_counter() - start) * 1000
        resp.raise_for_status()
        response_body = resp.json()

        await conn.execute(
            "INSERT INTO deployment_test_calls (tenant_id, deployment_id, request, response, latency_ms) "
            "VALUES ($1, $2, $3, $4, $5)",
            tenant_id,
            deployment_id,
            body,
            response_body,
            elapsed_ms,
        )
    return {"response": response_body, "latency_ms": elapsed_ms}


@router.get("/v1/projects/{project_id}/deployments/{deployment_id}/test-calls")
async def list_test_calls(
    project_id: uuid.UUID,
    deployment_id: uuid.UUID,
    tenant_id: str = Depends(get_tenant_id),
    limit: int = 20,
):
    async with TenantConn(tenant_id) as conn:
        owned = await conn.fetchval(
            "SELECT 1 FROM deployments WHERE id = $1 AND project_id = $2", deployment_id, project_id
        )
        if not owned:
            raise HTTPException(status_code=404, detail="deployment not found")
        rows = await conn.fetch(
            "SELECT id, request, response, latency_ms, created_at FROM deployment_test_calls "
            "WHERE deployment_id = $1 ORDER BY created_at DESC LIMIT $2",
            deployment_id,
            limit,
        )
    return [dict(r) for r in rows]


@router.post("/v1/predict/{deployment_id}")
async def predict(
    deployment_id: uuid.UUID,
    body: dict = Body(...),
    x_predict_key: str = Header(..., alias="X-Predict-Key"),
):
    # req/04: "separate auth scope from management API — this is what the
    # customer's own downstream systems call." Genuinely separate: no
    # tenant/JWT credential accepted here at all, just the per-deployment
    # secret handed back once at deploy time (see create_deployment). The
    # lookup runs SECURITY DEFINER (see migrations/0009) so this path
    # never needs a tenant session context.
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM deployment_for_predict($1, $2)", deployment_id, _hash_key(x_predict_key)
        )
    if row is None:
        raise HTTPException(status_code=401, detail="invalid deployment id or predict key")
    if row["status"] != "active":
        raise HTTPException(status_code=409, detail=f"deployment is {row['status']}, not active")
    if not await rate_limit.check(str(deployment_id), row["rate_limit_per_minute"]):
        raise HTTPException(
            status_code=429, detail=f"rate limit exceeded ({row['rate_limit_per_minute']}/min for this deployment)"
        )

    # FR6: an optional `metadata` passthrough field, popped before
    # forwarding to Serve (the model was never trained to expect it) and
    # echoed back alongside the prediction.
    metadata = body.pop("metadata", None)

    start = time.perf_counter()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{serve_client.RAY_SERVE_HTTP}{row['route_prefix']}", json=body)
    elapsed_ms = (time.perf_counter() - start) * 1000
    app_metrics.inference_latency_ms.record(elapsed_ms, {"deployment_id": str(deployment_id)})

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
            resource_type="inference",
            quantity=1,
            unit="requests",
            occurred_at=datetime.now(timezone.utc),
            source_ref=f"{deployment_id}:{uuid.uuid4()}",
        )

    result = resp.json()
    if metadata is not None:
        result["metadata"] = metadata
    return result
