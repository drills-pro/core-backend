"""The predict_calls log: one metadata row per key-authenticated call that reached the shared predictor apps, and its purge; the purge of try-it payloads."""
import logging
import uuid

from app.core.db import get_pool
from app.core.settings import settings
from app.core.tenant_conn import TenantConn
from app.integrations.serve_client import ServeErrorCode

logger = logging.getLogger(__name__)

_INSERT = (
    "INSERT INTO predict_calls (tenant_id, deployment_id, training_run_id, status, latency_ms, cold_load, error_code, request_bytes) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)"
)


async def record(
    tenant_id: str, deployment_id: uuid.UUID, run_id: uuid.UUID, *, status: int, latency_ms: float, cold_load: bool | None,
    error_code: ServeErrorCode | None, request_bytes: int,
) -> None:
    """Best effort: a log that cannot be written must not fail the call it describes."""
    try:
        async with TenantConn(tenant_id) as conn:
            await conn.execute(_INSERT, tenant_id, deployment_id, run_id, status, latency_ms, cold_load, error_code, request_bytes)
    except Exception:  # noqa: BLE001
        logger.exception("predict call of deployment %s not logged", deployment_id)


async def purge_test_calls() -> None:
    """Poller: drop try-it payloads older than serving.test_calls_retention_days."""
    async with (await get_pool()).acquire() as conn:
        dropped = await conn.fetchval("SELECT purge_test_calls($1)", settings.serving.test_calls_retention_days)
    if dropped:
        logger.info("purged %s test calls", dropped)


async def purge_predict_calls() -> None:
    """Poller: drop rows older than serving.predict_calls_retention_days."""
    async with (await get_pool()).acquire() as conn:
        dropped = await conn.fetchval("SELECT purge_predict_calls($1)", settings.serving.predict_calls_retention_days)
    if dropped:
        logger.info("purged %s predict calls", dropped)
