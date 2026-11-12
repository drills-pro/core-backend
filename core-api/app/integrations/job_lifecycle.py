"""A domain row that follows a Ray job: the one place that polls Ray, judges a deadline and moves the row.

Training runs, deployments and dataset imports each declare a `JobTable` (table, in-flight statuses, legal edges,
deadline) and a `settle(row, verdict)` step. This module owns the rest:

  claim    a lease on a batch of a tenant's in-flight rows (FOR UPDATE SKIP LOCKED), committed at once, so
           replicas running the same sweep get disjoint rows and no connection is held across a Ray call
  judge    Ray's answer plus the deadline: the single policy that lets a job Ray no longer answers for end
  advance  the only status UPDATE: a compare-and-set along a legal edge, so a double settle moves the row once
  backoff  a settle that raises keeps the row leased for an exponential delay (settle_attempts counts them) instead
           of letting the next tick retry it at once
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
# Rows a sweep polls and settles at once. Measured against Postgres with 20 ms Ray latency: 8 caps a replica near 270
# rows/s (a claim takes ~1 ms, so Ray is the limit). A row holds a pooled connection only for its short claim, settle
# write and lease write, never across Ray, so 16 sweeping rows need far fewer than 16 connections; the pool is the
# bound. Budget: every pod (core-api 1 + core-worker 2 in k8s apps/backend) can open pg.pool_max (20) connections, 3 x 20
# = 60 of Postgres' max_connections (100, minus 3 reserved for superusers and the Grafana/migrate sessions). A 5th pod
# would reach it (tests/integrations/test_job_lifecycle.py checks the budget): lower pool_max or add a pooler first.
SWEEP_CONCURRENCY = 16

# A settle that raises is not tried again until now + base * factor^(failures - 1), capped; it counts in the row's
# settle_attempts, which a settle that works (or a move) resets. At SETTLE_ERROR_AFTER failures it logs at ERROR once.
SETTLE_BACKOFF_BASE_SECONDS = 10
SETTLE_BACKOFF_FACTOR = 2
SETTLE_BACKOFF_MAX_SECONDS = 300
SETTLE_ERROR_AFTER = 5
LEASE_WRITE_TRIES = 2  # a lost connection fails the first write of a release or deferral: one more try


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
    return f"UPDATE {job.table} SET claimed_until = NULL, settle_attempts = 0 WHERE id = $1 AND claimed_until = $2"  # noqa: S608


def defer_sql(job: JobTable) -> str:
    return (  # noqa: S608
        f"UPDATE {job.table} SET claimed_until = now() + make_interval(secs => $3), settle_attempts = settle_attempts + 1 "
        "WHERE id = $1 AND claimed_until = $2"
    )


def settle_backoff(attempts: int) -> float:
    """Seconds a row waits after its `attempts`-th failed settle in a row."""
    return min(SETTLE_BACKOFF_BASE_SECONDS * SETTLE_BACKOFF_FACTOR ** (attempts - 1), SETTLE_BACKOFF_MAX_SECONDS)


_EMIT = "INSERT INTO events (tenant_id, project_id, kind, entity_id, status) VALUES ($1, $2, $3, $4, $5)"


async def emit(conn, job: JobTable, tenant_id, project_id, entity_id, status) -> None:
    """The outbox row for a job entering `status`; call it inside the transaction that wrote the row."""
    await conn.execute(_EMIT, tenant_id, project_id, f"{job.name}.{status}", entity_id, str(status))


def advance_sql(job: JobTable, columns: list[str], stamp_completed: bool) -> str:
    sets = ["status = $3", "claimed_until = NULL", "settle_attempts = 0"] + [f"{c} = ${i}" for i, c in enumerate(columns, start=4)]
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
    if moved is None:
        return None
    # The outbox row rides the same transaction as the move: no event without the change, none lost after it.
    await emit(conn, job, moved["tenant_id"], moved["project_id"], moved["id"], to)
    return dict(moved)


async def _write_lease(job: JobTable, row: dict, sql: str, *args) -> None:
    """Run a lease UPDATE for the lease this claim took, so it is a no-op after `advance` and never touches a later
    claimer's lease. A lost connection is retried once; after that the lease runs out by itself."""
    for attempt in range(1, LEASE_WRITE_TRIES + 1):
        try:
            async with TenantConn(str(row["tenant_id"])) as conn:
                await conn.execute(sql, row["id"], row["claimed_until"], *args)
            return
        except Exception:  # noqa: BLE001 - see the docstring
            if attempt == LEASE_WRITE_TRIES:
                logger.warning("could not update the claim on %s %s", job.table, row.get("id"), exc_info=True)


async def _release(job: JobTable, row: dict) -> None:
    """Make an unmoved row claimable again at once instead of when its lease runs out."""
    await _write_lease(job, row, release_sql(job))


async def _defer(job: JobTable, row: dict, attempts: int) -> None:
    """Keep a row whose settle raised unclaimable for its backoff."""
    await _write_lease(job, row, defer_sql(job), settle_backoff(attempts))


async def _follow(job: JobTable, settle: Settle, row: dict, now: datetime | None) -> None:
    attempts = 0  # failed settles in a row, this one included; 0 = this one worked
    try:
        job_id = row[job.ray_col]
        ray = await poll(job_id) if job_id else JobStatus.PENDING  # not submitted yet
        await settle(row, judge(job, row, ray, now or datetime.now(UTC)))
    except Exception:  # noqa: BLE001 - one bad row must not block the sweep; it is retried after its backoff
        attempts = int(row.get("settle_attempts") or 0) + 1
        logger.log(
            logging.ERROR if attempts == SETTLE_ERROR_AFTER else logging.WARNING,
            "failed to follow ray job of %s %s (failed settle %d in a row)", job.table, row.get("id"), attempts, exc_info=True,
        )
    finally:
        await (_defer(job, row, attempts) if attempts else _release(job, row))


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
