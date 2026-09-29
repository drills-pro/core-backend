"""Liveness and dependency-probe endpoints.

/health is the container's own signal; /health/deps reaches out to every
backing service so an operator can tell *which* dependency is down without
shelling into each container. Both are deliberately unauthenticated.
"""
import asyncpg
import boto3
import httpx
from fastapi import APIRouter

from app.core import config, vault

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/health/deps")
async def health_deps():
    return {
        "postgres": await _check_postgres(),
        "vault": _check_vault(),
        "lakefs_s3": _check_lakefs(),
        "zitadel": await _check_http(f"{config.ZITADEL_ISSUER}/.well-known/openid-configuration"),
        "ray": await _check_http(f"{config.RAY_DASHBOARD}/api/version"),
        "grafana": await _check_http(f"{config.GRAFANA_URL}/api/health"),
        "mlflow": await _check_http(f"{config.MLFLOW_URL}/health"),
        "labelstudio": await _check_http("http://labelstudio:8080/health"),
    }


async def _check_postgres() -> str:
    try:
        conn = await asyncpg.connect(config.DATABASE_URL, timeout=3)
        await conn.close()
        return "ok"
    except Exception as e:  # noqa: BLE001 - health check, report any failure
        return f"error: {e}"


def _check_vault() -> str:
    # Reads this app's own zitadel secret (app-secret/app-backend/zitadel) rather than
    # platform-secret's — this is a Vault-reachability probe, not a real dependency on
    # Zitadel's server masterkey, so it should only ever touch what backend actually owns.
    try:
        vault.get_kv("zitadel")
        return "ok"
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def _check_lakefs() -> str:
    # Deliberately a raw boto3 client rather than app/integrations/lakefs_client:
    # the point is to prove the credentials and endpoint work right now, not to
    # exercise the cached client the rest of the app shares.
    try:
        creds = vault.get_kv("lakefs")
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
