"""A domain row that follows a Ray job: the one place that polls Ray, judges a deadline and moves the row.

Training runs, deployments and dataset imports each declare a `JobTable` (table, in-flight statuses, legal edges,
deadline) and a `settle(row, verdict)` step. This module owns the rest:

  claim    a lease on a batch of a tenant's in-flight rows (FOR UPDATE SKIP LOCKED), committed at once, so
           replicas running the same sweep get disjoint rows and no connection is held across a Ray call
  judge    Ray's answer plus the deadline: the single policy that lets a job Ray no longer answers for end
  advance  the only status UPDATE: a compare-and-set along a legal edge, so a double settle moves the row once
  stop_quietly  the best-effort stop of a job a context ends on its own
  sweep    the tenant loop: claim, then up to SWEEP_CONCURRENCY rows polled and settled at a time

`settle` gets no connection: it decides, runs its effects (gateways, MLflow, S3) with none held, then persists in
a short `TenantConn` of its own. The lease only recovers from a crashed replica; correctness rests on `advance`
and on every effect being idempotent.
"""
import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.db import get_pool
from app.core.tenant_conn import TenantConn
from app.integrations import compute
from app.integrations.compute import JobStatus

logger = logging.getLogger(__name__)

# Longer than the slowest settle (a model upload has 600s read/write timeouts) so a live settle is never re-claimed;
# a crashed replica's rows become claimable again after this.
CLAIM_LEASE_SECONDS = 900
CLAIM_BATCH_MAX = 20  # rows of one tenant claimed per sweep
SWEEP_CONCURRENCY = 8  # rows a replica polls and settles at once


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


class Verdict(BaseModel):
    """What the sweep tells `settle`: Ray's state (None = no usable answer) and whether the job is out of time."""

    model_config = ConfigDict(frozen=True)

    ray: JobStatus | None
    phase: Phase | None
    timed_out: bool


class JobTable(BaseModel):
    """One context's rows that follow a Ray job. The names are the context's own constants, never user input, so
    they are safe to put in SQL; `columns` is every extra column `advance` may set."""

    model_config = ConfigDict(frozen=True)

    name: str
    table: str
    tenants_fn: str  # SECURITY DEFINER function listing the tenants with in-flight rows
    inflight: tuple[str, ...]
    transitions: Mapping[str, frozenset[str]]
    columns: frozenset[str] = frozenset()
    ray_col: str = "ray_job_id"
    completed_col: str | None = "completed_at"
    deadline: Callable[[dict], datetime | None]  # None = no deadline for this row


Settle = Callable[[dict, Verdict], Awaitable[None]]


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
    except Exception:  # noqa: BLE001 - judge decides what no answer means
        logger.warning("no usable status for ray job %s", job_id, exc_info=True)
        return None


async def stop_quietly(job_id: str) -> None:
    """Stop the Ray job; never raises (it may already be gone, and the row has ended either way)."""
    try:
        await compute.stop(job_id)
    except Exception:  # noqa: BLE001 - see above
        logger.warning("could not stop ray job %s", job_id)


def judge(job: JobTable, row: dict, ray: JobStatus | None, now: datetime) -> Verdict:
    """A job Ray no longer answers for, or that overran its deadline, can always end; a terminal answer wins."""
    ph = phase(ray) if ray is not None else None
    deadline = job.deadline(row)
    timed_out = deadline is not None and now >= deadline and ph in (None, Phase.IN_FLIGHT)
    return Verdict(ray=ray, phase=ph, timed_out=timed_out)


def tenants_sql(job: JobTable) -> str:
    return f"SELECT {job.tenants_fn}()"  # noqa: S608


def claim_sql(job: JobTable) -> str:
    # MATERIALIZED: an `id IN (SELECT ... LIMIT .. FOR UPDATE SKIP LOCKED)` can plan as a nested loop that re-runs the
    # subquery per outer row, so one claim leases (and returns) far more than LIMIT. The CTE is evaluated exactly once.
    return (  # noqa: S608
        f"WITH picked AS MATERIALIZED (SELECT id FROM {job.table} "
        "WHERE tenant_id = $1 AND status = ANY($2) AND (claimed_until IS NULL OR claimed_until < now()) "
        "ORDER BY created_at LIMIT $4 FOR UPDATE SKIP LOCKED) "
        f"UPDATE {job.table} t SET claimed_until = now() + make_interval(secs => $3) "
        "FROM picked WHERE t.id = picked.id RETURNING t.*"
    )


def release_sql(job: JobTable) -> str:
    return f"UPDATE {job.table} SET claimed_until = NULL WHERE id = $1 AND claimed_until = $2"  # noqa: S608


def advance_sql(job: JobTable, columns: list[str], stamp_completed: bool) -> str:
    sets = ["status = $3", "claimed_until = NULL"] + [f"{c} = ${i}" for i, c in enumerate(columns, start=4)]
    if stamp_completed:
        sets.append(f"{job.completed_col} = ${len(columns) + 4}")
    return f"UPDATE {job.table} SET {', '.join(sets)} WHERE id = $1 AND status = $2 RETURNING *"  # noqa: S608


async def claim(conn, job: JobTable, tenant_id: str, limit: int) -> list[dict]:
    """Lease up to `limit` of the tenant's in-flight rows that nobody else holds. Run in a TenantConn that commits
    right away; two replicas claiming at once get disjoint rows."""
    rows = await conn.fetch(claim_sql(job), tenant_id, [str(s) for s in job.inflight], float(CLAIM_LEASE_SECONDS), limit)
    return [dict(r) for r in rows]


async def advance(conn, job: JobTable, row: dict, to: str, **cols: Any) -> dict | None:
    """Move the row from the status it was read at to `to`, setting `cols`. None when that is not a legal edge or
    someone moved the row first. A terminal status also stamps `completed_col` (read it from the returned row)."""
    unknown = cols.keys() - job.columns
    if unknown:
        raise ValueError(f"{job.table}: advance may not set {sorted(unknown)}")
    frm = row["status"]
    if to not in job.transitions.get(frm, frozenset()):
        return None
    stamp = job.completed_col is not None and not job.transitions.get(to)
    args = [row["id"], frm, to, *cols.values()]
    if stamp:
        args.append(datetime.now(UTC))
    moved = await conn.fetchrow(advance_sql(job, list(cols), stamp), *args)
    return dict(moved) if moved is not None else None


async def _release(job: JobTable, row: dict) -> None:
    """Make an unmoved row claimable again at once instead of when its lease runs out. Clears only the lease this
    claim took, so a no-op after `advance` and never wipes a later claimer's lease."""
    try:
        async with TenantConn(str(row["tenant_id"])) as conn:
            await conn.execute(release_sql(job), row["id"], row["claimed_until"])
    except Exception:  # noqa: BLE001 - the lease expires by itself
        logger.warning("could not release the claim on %s %s", job.table, row.get("id"), exc_info=True)


async def _follow(job: JobTable, settle: Settle, row: dict, now: datetime | None) -> None:
    try:
        job_id = row[job.ray_col]
        ray = await poll(job_id) if job_id else JobStatus.PENDING  # not submitted yet
        await settle(row, judge(job, row, ray, now or datetime.now(UTC)))
    except Exception:  # noqa: BLE001 - one bad row must not block the sweep; the lease expires and it is retried
        logger.exception("failed to follow ray job of %s %s", job.table, row.get("id"))
    finally:
        await _release(job, row)


async def sweep(job: JobTable, settle: Settle, now: datetime | None = None) -> None:
    """Follow every in-flight row of every tenant, SWEEP_CONCURRENCY at a time. Every replica may run this: a claim
    gives each row to one of them. `now` is a parameter so tests can move the clock."""
    running: set[asyncio.Task] = set()
    try:
        for tenant_id in [str(r[0]) for r in await fetch(tenants_sql(job))]:
            budget = CLAIM_BATCH_MAX  # ponytail: oldest CLAIM_BATCH_MAX per tenant per sweep; rotate if one tenant ever has more in flight
            while budget > 0:
                room = SWEEP_CONCURRENCY - len(running)
                if room <= 0:
                    await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                    continue
                limit = min(budget, room)
                async with TenantConn(tenant_id) as conn:
                    rows = await claim(conn, job, tenant_id, limit)
                for row in rows:
                    task = asyncio.create_task(_follow(job, settle, row, now))
                    running.add(task)
                    task.add_done_callback(running.discard)
                if len(rows) < limit:
                    break
                budget -= limit
        await asyncio.gather(*running)
    finally:
        for task in running:
            task.cancel()
