import asyncio
import json
import logging

import asyncpg

from app.core import config, vault

logger = logging.getLogger(__name__)

PG_HOST = config.PG_HOST
PG_PORT = config.PG_PORT
PG_DATABASE = config.PG_DATABASE

_pool: asyncpg.Pool | None = None
_refresh_task: asyncio.Task | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        await _rotate()
        _start_refresh_loop()
    return _pool


async def _rotate() -> int:
    """Issue fresh Vault-backed credentials, swap the pool, return the lease TTL."""
    global _pool
    username, password, lease_seconds = vault.get_db_creds()
    dsn = f"postgresql://{username}:{password}@{PG_HOST}:{PG_PORT}/{PG_DATABASE}"

    async def _init(conn):
        # Without this, jsonb columns (training_runs.config/metrics/model_artifact,
        # etc.) come back as raw JSON text, not parsed objects — every caller
        # would otherwise need its own json.loads.
        await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

    new_pool = await asyncpg.create_pool(dsn, init=_init)
    old_pool, _pool = _pool, new_pool
    if old_pool is not None:
        await old_pool.close()
    logger.info("rotated db creds, lease=%ss", lease_seconds)
    return lease_seconds


def _start_refresh_loop() -> None:
    global _refresh_task
    if _refresh_task is None:
        _refresh_task = asyncio.create_task(_refresh_loop())


async def _refresh_loop() -> None:
    # ponytail: re-fetch at 50% of the lease TTL rather than tracking Vault's
    # renew API precisely — simple and plenty for a dev-mode Vault. Swap for
    # proper lease renewal if a real Vault deployment's TTL policy needs it.
    while True:
        lease_seconds = await _rotate()
        await asyncio.sleep(max(lease_seconds // 2, 30))
