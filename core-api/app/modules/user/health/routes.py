"""Liveness and dependency-probe endpoints.

/health is the container's own signal; /health/deps reaches out to every
backing service so an operator can tell *which* dependency is down without
shelling into each container. Both are deliberately unauthenticated.
"""
import boto3
import httpx
from fastapi import APIRouter

from app.core import db, secret_files, vault
from app.core.settings import settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/health/deps")
async def health_deps():
    return {
        "postgres": await _check_postgres(),
        "vault": _check_vault(),
        "secrets": _check_secrets(),
        "lakefs_s3": _check_lakefs(),
        "zitadel": await _check_http(f"{settings.zitadel.issuer}/.well-known/openid-configuration"),
        "ray": await _check_http(f"{settings.ray.dashboard}/api/version"),
        "mlflow": await _check_http(f"{settings.mlflow.url}/health"),
        "labelstudio": await _check_labelstudio(),
    }


async def _check_postgres() -> str:
    # Reuses the app's own connection pool (dynamic Vault-issued creds, database/creds/core-app)
    # instead of a separate static superuser DSN — proves the real data path works, not a
    # parallel one the app-backend Vault role was never even authorized to use.
    try:
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
        return "ok"
    except Exception as e:  # noqa: BLE001 - health check, report any failure
        return f"error: {e}"


def _check_vault() -> str:
    try:
        return "ok" if vault.is_authenticated() else "error: token rejected"
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def _check_secrets() -> str:
    # Every mounted secret file loads and has the keys its consumers rely on.
    bad = []
    for name in secret_files.REQUIRED:
        if name == "zitadel" and settings.env != "prod":
            continue  # local Zitadel is reached with the PAT volume, not a service-user key
        if name == "zitadel_oidc" and settings.zitadel.project_id:
            continue  # the project id is configured; this file is only a local-dev fallback
        try:
            secret_files.get(name)
        except secret_files.SecretError as e:
            bad.append(str(e))
    return "ok" if not bad else "error: " + "; ".join(bad)


def _check_lakefs() -> str:
    # Deliberately a raw boto3 client rather than app/integrations/lakefs_client:
    # the point is to prove the credentials and endpoint work right now, not to
    # exercise the cached client the rest of the app shares.
    try:
        creds = secret_files.get("lakefs")
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


async def _check_labelstudio() -> str:
    try:
        return await _check_http(f"{secret_files.get('labelstudio')['url']}/health")
    except secret_files.SecretError as e:
        return f"error: {e}"


async def _check_http(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        return "ok"
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"
