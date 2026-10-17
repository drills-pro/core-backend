import asyncio
import json
import logging

import asyncpg

from app.core import secret_files
from app.core.settings import settings

logger = logging.getLogger(__name__)

PG = settings.pg
# The Vault agent writes a new user well before the old lease expires, so a 30s poll is enough.
WATCH_SECONDS = 30

_pool: asyncpg.Pool | None = None
_pool_creds: dict | None = None
_watch_task: asyncio.Task | None = None
_pool_lock = asyncio.Lock()


async def get_pool() -> asyncpg.Pool:
    global _watch_task
    if _pool is None:
        # Single-flight: concurrent first requests would otherwise each build a pool, leaking all but the last.
        async with _pool_lock:
            if _pool is None:
                await _rotate(secret_files.get("db"))
                _watch_task = asyncio.create_task(_watch())
    return _pool


async def _rotate(creds: dict) -> None:
    """Open a pool with these credentials (the `db` secret file) and swap it in."""
    global _pool, _pool_creds
    dsn = f"postgresql://{creds['username']}:{creds['password']}@{PG.host}:{PG.port}/{PG.database}"

    async def _init(conn):
        # Without this, jsonb columns (training_runs.config/metrics/model_artifact,
        # etc.) come back as raw JSON text, not parsed objects — every caller
        # would otherwise need its own json.loads.
        await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

    new_pool = await asyncpg.create_pool(
        dsn,
        init=_init,
        min_size=PG.pool_min,
        max_size=PG.pool_max,
        command_timeout=PG.command_timeout,
    )
    old_pool, _pool, _pool_creds = _pool, new_pool, creds
    if old_pool is not None:
        await old_pool.close()
    logger.info("db pool opened as %s", creds["username"])


async def _watch() -> None:
    while True:
        await asyncio.sleep(WATCH_SECONDS)
        try:
            creds = secret_files.get("db")  # one stat() unless the file changed
            if creds != _pool_creds:
                await _rotate(creds)
        except Exception:  # noqa: BLE001 - keep serving on the old pool; the next tick retries
            logger.exception("db credential rotation failed; retrying in %ss", WATCH_SECONDS)
