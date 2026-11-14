"""Liveness and dependency-probe endpoints.

/health is the container's own signal; /health/deps reaches out to every
service the API needs to do its job so an operator can tell *which* dependency is down without
shelling into each container. Both are deliberately unauthenticated.
"""
import httpx
from fastapi import APIRouter, Depends

from app.core import db, secret_files
from app.core.authz import public
from app.core.settings import settings
from app.integrations import data_plane

router = APIRouter(tags=["health"], dependencies=[Depends(public)])


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/health/deps")
async def health_deps():
    return {
        "postgres": await _check_postgres(),
        "secrets": _check_secrets(),
        "lakefs_s3": await data_plane.lakefs_health(),
        "zitadel": await _check_http(f"{settings.zitadel.issuer}/.well-known/openid-configuration"),
        "ray": await _check_http(f"{settings.ray.dashboard}/api/version"),
        "mlflow": await data_plane.mlflow_health(),
    }


async def _check_postgres() -> str:
    # The app's own pool (the `db` secret file), so this checks the real data path.
    try:
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
        return "ok"
    except Exception as e:  # noqa: BLE001 - health check, report any failure
        return f"error: {e}"


def _check_secrets() -> str:
    # Every mounted secret file loads and has the keys its consumers rely on; the lakeFS and staging
    # files are checked by the data plane's own health probe.
    skip = secret_files.DATA_PLANE + (("zitadel",) if settings.env != "prod" else ())  # local Zitadel uses the PAT volume
    skip += () if settings.mail.enabled else (secret_files.ZOHO,)  # no mail, no SMTP credential to mount
    bad = secret_files.problems(skip)
    return "ok" if not bad else "error: " + "; ".join(bad)


async def _check_http(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        return "ok"
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"
