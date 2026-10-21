"""Everything this process does on a timer.

Part of the composition root, not of any one module: each module owns the
reconcile function, this file owns the decision to run it every N seconds.
A poller that raises never takes the app down — it logs and waits for the
next tick, which is what every one of these did inline in main.py before.
"""
import asyncio
import logging
import zlib

import httpx

from app.core import metrics, rate_limit
from app.core.settings import settings
from app.core.db import get_pool
from app.modules.user.datasets import reconcile_inflight_imports
from app.modules.user.deployments import reconcile_inflight_deployments
from app.modules.user.preprocessing import reconcile_inflight_preprocessing
from app.modules.user.training import reconcile_inflight_runs

logger = logging.getLogger(__name__)

RECONCILE_INTERVAL_SECONDS = 10
TELEMETRY_INTERVAL_SECONDS = 30


async def _poll_pg_size() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        size = await conn.fetchval("SELECT pg_database_size(current_database())")
    metrics.set_pg_size_bytes(size)


async def _poll_gpu_quota() -> None:
    # req/08 FR2 GPU-exhaustion signal: scrapes kube-state-metrics' real
    # kube_resourcequota series for requests.nvidia.com/gpu (see
    # k8s/tenant-quota-example.yaml). Plain-text Prometheus exposition
    # format — parsed by hand rather than pulling in a client library for
    # two label values.
    if not settings.observability.ksm_endpoint:
        return  # not configured (local dev, or no kube-state-metrics to scrape)
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(settings.observability.ksm_endpoint)
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


async def _poll_active_principals() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        counts = {
            window: await conn.fetchval(
                "SELECT count(*) FROM principals WHERE last_seen_at > now() - make_interval(days => $1)", days
            )
            for window, days in metrics.ACTIVE_PRINCIPAL_WINDOW_DAYS.items()
        }
    metrics.set_active_principals(counts)


# (name, coroutine function, seconds between runs)
POLLERS = (
    ("pg-size", _poll_pg_size, TELEMETRY_INTERVAL_SECONDS),
    ("gpu-quota", _poll_gpu_quota, TELEMETRY_INTERVAL_SECONDS),
    ("active-principals", _poll_active_principals, TELEMETRY_INTERVAL_SECONDS),
    ("training-reconcile", reconcile_inflight_runs, RECONCILE_INTERVAL_SECONDS),
    ("dataset-import-reconcile", reconcile_inflight_imports, RECONCILE_INTERVAL_SECONDS),
    ("deployment-reconcile", reconcile_inflight_deployments, RECONCILE_INTERVAL_SECONDS),
    ("preprocessing-reconcile", reconcile_inflight_preprocessing, RECONCILE_INTERVAL_SECONDS),
    ("rate-limit-purge", rate_limit.purge, 60),
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


# Gauges every replica must export itself; electing a leader would leave the others at 0.
PER_REPLICA = {"pg-size", "gpu-quota"}


async def _run_forever(name: str, poll, interval: int) -> None:
    while True:
        try:
            if name in PER_REPLICA:
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
