"""A domain row that follows a Ray job: the sweep every context's background reconciler shares.

The context supplies the SECURITY DEFINER function that lists tenants with work, the query for a tenant's rows,
and a `settle` step that moves one row given Ray's state. This module owns the tenant loop, one transaction per row,
and the isolation of a failing row, so one bad job never blocks the sweep.
"""
import logging
from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum
from typing import Any

from app.core.db import get_pool
from app.core.tenant_conn import TenantConn
from app.integrations import compute
from app.integrations.compute import JobStatus

logger = logging.getLogger(__name__)


class Phase(StrEnum):
    IN_FLIGHT = "in_flight"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"  # someone stopped the job: terminal, but not a failure of the job itself


_PHASE = {
    JobStatus.PENDING: Phase.IN_FLIGHT,
    JobStatus.RUNNING: Phase.IN_FLIGHT,
    JobStatus.SUCCEEDED: Phase.SUCCEEDED,
    JobStatus.FAILED: Phase.FAILED,
    JobStatus.STOPPED: Phase.STOPPED,
}


def phase(status: JobStatus) -> Phase:
    return _PHASE[status]


async def fetch(sql: str, *args) -> list:
    """A cross-tenant listing (SECURITY DEFINER function), before any tenant is set."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(sql, *args)


async def poll(job_id: str) -> JobStatus | None:
    """Ray's state of the job; None when Ray does not answer or answers something unknown (the job was
    garbage-collected, the head restarted)."""
    try:
        return JobStatus(await compute.status(job_id))
    except Exception:  # noqa: BLE001 - the caller decides what no answer means
        logger.warning("no usable status for ray job %s", job_id, exc_info=True)
        return None


async def follow(
    tenants_sql: str,
    select: Callable[[Any, Any], Awaitable[Sequence]],  # (conn, tenant_id) -> rows; every row carries an `id`
    settle: Callable[[Any, dict], Awaitable[None]],  # (conn, row)
) -> None:
    """For each tenant `tenants_sql` lists: `select(conn, tenant_id)` its rows, then `settle(conn, row)` each in its
    own tenant transaction."""
    for tenant_id in [r[0] for r in await fetch(tenants_sql)]:
        async with TenantConn(str(tenant_id)) as conn:
            rows = [dict(r) for r in await select(conn, tenant_id)]
        for row in rows:
            try:
                async with TenantConn(str(tenant_id)) as conn:
                    await settle(conn, row)
            except Exception:  # noqa: BLE001 - one bad row shouldn't block the sweep
                logger.exception("failed to follow ray job of row %s", row.get("id"))
