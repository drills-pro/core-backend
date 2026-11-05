"""Everything this process does on a timer.

Part of the composition root, not of any one module: each module owns the
reconcile function, this file owns the decision to run it every N seconds.
A poller that raises never takes the app down — it logs and waits for the
next tick, which is what every one of these did inline in main.py before.
"""
import asyncio
import logging
import zlib

from app.core import rate_limit
from app.core.db import get_pool
from app.integrations.serve_client import ensure_serving
from app.modules.user.datasets import expire_stale_imports, purge_deleted_datasets, reconcile_inflight_imports
from app.modules.user.deployments import purge_predict_calls, reconcile_inflight_deployments
from app.modules.user.training import reconcile_inflight_runs

logger = logging.getLogger(__name__)

RECONCILE_INTERVAL_SECONDS = 10
SWEEP_INTERVAL_SECONDS = 60
PURGE_INTERVAL_SECONDS = 3600
SERVING_ENSURE_INTERVAL_SECONDS = 60


# (name, coroutine function, seconds between runs)
POLLERS = (
    ("training-reconcile", reconcile_inflight_runs, RECONCILE_INTERVAL_SECONDS),
    ("deployment-reconcile", reconcile_inflight_deployments, RECONCILE_INTERVAL_SECONDS),
    ("import-reconcile", reconcile_inflight_imports, RECONCILE_INTERVAL_SECONDS),
    ("import-expiry", expire_stale_imports, SWEEP_INTERVAL_SECONDS),
    ("dataset-purge", purge_deleted_datasets, PURGE_INTERVAL_SECONDS),
    ("rate-limit-purge", rate_limit.purge, SWEEP_INTERVAL_SECONDS),
    ("serving-ensure", ensure_serving, SERVING_ENSURE_INTERVAL_SECONDS),
    ("predict-calls-purge", purge_predict_calls, PURGE_INTERVAL_SECONDS),
)


async def _run_as_leader(name: str, poll) -> None:
    """Run `poll` only if no other replica is running it right now.

    A session-level Postgres advisory lock, held on one pooled connection for the length of the
    run, so with N replicas each sweep still happens once. If this process dies the connection
    closes and the lock goes with it — nothing to expire or clean up.
    """
    lock_id = zlib.crc32(name.encode())
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", lock_id):
            return
        try:
            await poll()
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", lock_id)


# Pollers every replica runs: each claims rows of its own (app/integrations/job_lifecycle.py), so replicas share the work.
# The rest sweep cheaply and elect one leader.
CLAIMING = {"training-reconcile", "deployment-reconcile", "import-reconcile"}


async def _run_forever(name: str, poll, interval: int) -> None:
    while True:
        try:
            if name in CLAIMING:
                await poll()
            else:
                await _run_as_leader(name, poll)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - best-effort, never crash the app
            logger.exception("poller %s failed", name)
        await asyncio.sleep(interval)


def start() -> list[asyncio.Task]:
    return [asyncio.create_task(_run_forever(*p), name=f"poller:{p[0]}") for p in POLLERS]


async def stop(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
