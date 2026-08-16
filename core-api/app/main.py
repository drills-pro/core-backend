import asyncio
import os

import asyncpg
import boto3
import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import metrics, tracing, vault
from app.datasets import reconcile_inflight_imports
from app.datasets import router as datasets_router
from app.db import get_pool
from app.deployments import reconcile_inflight_deployments
from app.deployments import router as deployments_router
from app.embedded_auth import router as embedded_auth_router
from app.labeling import router as labeling_router
from app.models import router as models_router
from app.project_members import router as project_members_router
from app.preprocessing import reconcile_inflight_preprocessing
from app.preprocessing import router as preprocessing_router
from app.projects import router as projects_router
from app.public import router as public_router
from app.teams import router as teams_router
from app.training import reconcile_inflight_runs
from app.training import router as training_router
from app.users import router as users_router
from app.usage import router as usage_router

app = FastAPI(title="core-api", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    # Wildcard is safe here: the frontend authenticates with a Bearer
    # header, not cookies, so allow_credentials stays off (browsers
    # reject "*" + credentials combos anyway).
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
tracing.setup(app)
app.include_router(projects_router)
app.include_router(datasets_router)
app.include_router(deployments_router)
app.include_router(embedded_auth_router)
app.include_router(labeling_router)
app.include_router(models_router)
app.include_router(project_members_router)
app.include_router(preprocessing_router)
app.include_router(public_router)
app.include_router(teams_router)
app.include_router(training_router)
app.include_router(users_router)
app.include_router(usage_router)
app.mount("/ui", StaticFiles(directory="ui", html=True), name="ui")


@app.on_event("startup")
async def _start_pg_size_poller():
    async def loop():
        while True:
            try:
                pool = await get_pool()
                async with pool.acquire() as conn:
                    size = await conn.fetchval("SELECT pg_database_size('core')")
                metrics.set_pg_size_bytes(size)
            except Exception:  # noqa: BLE001 - best-effort telemetry, never crash the app
                pass
            await asyncio.sleep(30)

    asyncio.create_task(loop())


@app.on_event("startup")
async def _start_training_reconcile_poller():
    async def loop():
        while True:
            try:
                await reconcile_inflight_runs()
            except Exception:  # noqa: BLE001 - best-effort, never crash the app
                pass
            await asyncio.sleep(10)

    asyncio.create_task(loop())


@app.on_event("startup")
async def _start_import_reconcile_poller():
    async def loop():
        while True:
            try:
                await reconcile_inflight_imports()
            except Exception:  # noqa: BLE001 - best-effort, never crash the app
                pass
            await asyncio.sleep(10)

    asyncio.create_task(loop())


@app.on_event("startup")
async def _start_deployment_reconcile_poller():
    async def loop():
        while True:
            try:
                await reconcile_inflight_deployments()
            except Exception:  # noqa: BLE001 - best-effort, never crash the app
                pass
            await asyncio.sleep(10)

    asyncio.create_task(loop())


@app.on_event("startup")
async def _start_preprocessing_reconcile_poller():
    async def loop():
        while True:
            try:
                await reconcile_inflight_preprocessing()
            except Exception:  # noqa: BLE001 - best-effort, never crash the app
                pass
            await asyncio.sleep(10)

    asyncio.create_task(loop())


KSM_ENDPOINT = os.environ.get("KSM_ENDPOINT", "http://192.168.117.2:32581/metrics")


@app.on_event("startup")
async def _start_gpu_quota_poller():
    # req/08 FR2 GPU-exhaustion signal: scrapes kube-state-metrics' real
    # kube_resourcequota series for requests.nvidia.com/gpu (see
    # k8s/tenant-quota-example.yaml). Plain-text Prometheus exposition
    # format — parsed by hand rather than pulling in a client library for
    # two label values.
    async def loop():
        while True:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.get(KSM_ENDPOINT)
                    resp.raise_for_status()
                used = hard = 0
                for line in resp.text.splitlines():
                    if 'resource="requests.nvidia.com/gpu"' not in line:
                        continue
                    value = int(float(line.rsplit(" ", 1)[-1]))
                    if 'type="used"' in line:
                        used = value
                    elif 'type="hard"' in line:
                        hard = value
                metrics.set_gpu_quota(used, hard)
            except Exception:  # noqa: BLE001 - best-effort telemetry, never crash the app
                pass
            await asyncio.sleep(30)

    asyncio.create_task(loop())

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://core:core@postgres:5432/core")
ZITADEL_ISSUER = os.environ.get("ZITADEL_ISSUER", "http://zitadel:8080")
RAY_DASHBOARD = os.environ.get("RAY_DASHBOARD", "http://ray-head:8265")
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://lgtm:3000")
MLFLOW_URL = os.environ.get("MLFLOW_URL", "http://mlflow:5000")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/health/deps")
async def health_deps():
    return {
        "postgres": await _check_postgres(),
        "vault": _check_vault(),
        "lakefs_s3": _check_lakefs(),
        "zitadel": await _check_http(f"{ZITADEL_ISSUER}/.well-known/openid-configuration"),
        "ray": await _check_http(f"{RAY_DASHBOARD}/api/version"),
        "grafana": await _check_http(f"{GRAFANA_URL}/api/health"),
        "mlflow": await _check_http(f"{MLFLOW_URL}/health"),
        "labelstudio": await _check_http("http://labelstudio:8080/health"),
    }


async def _check_postgres() -> str:
    try:
        conn = await asyncpg.connect(DATABASE_URL, timeout=3)
        await conn.close()
        return "ok"
    except Exception as e:  # noqa: BLE001 - health check, report any failure
        return f"error: {e}"


def _check_vault() -> str:
    try:
        vault.get_kv("platform/zitadel")
        return "ok"
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def _check_lakefs() -> str:
    try:
        creds = vault.get_kv("platform/lakefs")
        client = boto3.client(
            "s3",
            endpoint_url=creds["endpoint"],
            aws_access_key_id=creds["access_key"],
            aws_secret_access_key=creds["secret_key"],
        )
        client.list_buckets()
        return "ok"
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


async def _check_http(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        return "ok"
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"
