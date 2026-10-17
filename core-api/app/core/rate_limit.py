"""req/04 FR4: rate limiting on the inference endpoint per deployment (and on signup).

Fixed one-minute windows counted in Postgres, so every core-api replica shares the count. One
atomic upsert per check. A fixed window allows up to 2x the limit across a minute boundary,
which is fine for abuse protection.
# ponytail: one DB round-trip per check. If predict traffic outgrows that, swap this body for
# Redis/Dragonfly INCR+EXPIRE — callers only see `await check(key, limit)`.
"""
import time

from app.core.db import get_pool


async def check(key: str, limit_per_minute: int) -> bool:
    """Returns True if the request is allowed, False if it should be 429'd."""
    window = int(time.time() // 60)
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "INSERT INTO rate_limits (key, minute, count) VALUES ($1, $2, 1) "
            "ON CONFLICT (key, minute) DO UPDATE SET count = rate_limits.count + 1 RETURNING count",
            key,
            window,
        )
    return count <= limit_per_minute


async def purge() -> None:
    """Poller: drop windows nobody will read again."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM rate_limits WHERE minute < $1", int(time.time() // 60) - 5)
