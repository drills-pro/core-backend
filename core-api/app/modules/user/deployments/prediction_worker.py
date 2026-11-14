"""The prediction worker and the retention purge (planning/24), run by the pollers in app/background.py.

`process_predictions` claims due background predictions of every tenant with work (a lease, attempt + 1, FOR UPDATE SKIP LOCKED, committed
at once), sends them to Serve concurrently (asyncio.gather bounded by predict.worker_concurrency, so a burst fans out across Serve's
replicas) and writes each result back fenced on the attempt it claimed: a worker that lost its lease writes nothing. A 5xx, a timeout or a
lost lease goes back to queued with a backoff until predict.max_attempts; a 4xx fails at once. No connection is held across a Serve call.
A key's cancel is honoured before each attempt and every CANCEL_CHECK_SECONDS while Serve is called (the call is dropped); a result that
arrives after the cancel flag is discarded.
"""
import asyncio
import json
import logging
import uuid
from http import HTTPStatus

from app.core.db import get_pool
from app.core.problem import Problem
from app.core.settings import settings
from app.core.tenant_conn import TenantConn
from app.integrations import job_lifecycle
from app.integrations import data_plane
from app.modules.user.deployments import model_predict, prediction_store as store
from app.modules.user.deployments.constants import (
    CANCEL_CHECK_SECONDS,
    DEPLOYMENT_GONE_DETAIL,
    LEASE_LOST_DETAIL,
    PREDICTION_RETENTION_PURGE_LOG,
    RETRY_BACKOFF_SECONDS,
    UPSTREAM_ERROR_DETAIL,
    WORKER_SWEEP_MAX_ROUNDS,
    PredictionStatus,
    ProblemCode,
)
from app.modules.user.deployments.schemas import PredictionError
from app.modules.user.deployments.status import DeploymentStatus

logger = logging.getLogger(__name__)

WORKER_ID = uuid.uuid4().hex  # who holds a lease: this process, this run
_TENANTS = "SELECT inflight_prediction_tenant_ids()"
_EXPIRING_REFS = "SELECT id, tenant_id, project_id FROM expiring_prediction_refs()"
_PURGE = "SELECT purge_predictions($1::uuid[])"
_DEPLOYMENT = "SELECT id, status, training_run_id FROM deployments WHERE id = $1"


def backoff_seconds(attempt: int) -> float:
    """How long a prediction whose `attempt`-th try failed waits for the next one."""
    return RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS)) - 1]


def _retryable(problem: Problem) -> bool:
    return problem.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR or problem.status_code == HTTPStatus.TOO_MANY_REQUESTS


async def _retry_or_fail(conn, row, error: PredictionError, retryable: bool):
    if retryable and row["attempt"] < row["max_attempts"]:
        return await store.requeue(conn, row, backoff_seconds(row["attempt"]))
    return await store.finish(conn, row, PredictionStatus.FAILED, error=error)


class _Cancelled(Exception):
    """The key cancelled the prediction while its attempt was running."""


async def _written(row, write) -> None:
    """Run `write(conn)` (a fenced write-back) in a short transaction of the row's tenant, unless the key cancelled the attempt: then the
    prediction ends cancelled whatever `write` would have said (a result after the cancel flag is discarded). Say so when the fence dropped it."""
    async with TenantConn(str(row["tenant_id"])) as conn:
        if await store.cancel_requested(conn, row, lock=True):
            moved = await store.finish(conn, row, PredictionStatus.CANCELLED)
        else:
            moved = await write(conn)
    if moved is None:
        logger.info("prediction %s attempt %s was taken over; its write-back is dropped", row["id"], row["attempt"])


async def _call(row, deployment) -> tuple[object, float]:
    """The Serve call of this attempt, dropped (httpx cancel) as soon as a poll finds the key asked to cancel."""
    tenant_id = str(row["tenant_id"])
    input_ = await store.payload(row, "input")
    call = asyncio.create_task(model_predict.serve_call(
        tenant_id, row["project_id"], deployment, input_, len(json.dumps(input_)), settings.predict.worker_concurrency))
    try:
        while True:
            done, _ = await asyncio.wait({call}, timeout=CANCEL_CHECK_SECONDS)
            if done:
                return call.result()
            async with TenantConn(tenant_id) as conn:
                if await store.cancel_requested(conn, row):
                    raise _Cancelled
    finally:
        if not call.done():
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)


async def _attempt(row) -> None:
    tenant_id = str(row["tenant_id"])
    if row["cancel_requested"]:  # asked while it waited for a retry: no call at all
        return await _written(row, lambda conn: store.finish(conn, row, PredictionStatus.CANCELLED))
    async with TenantConn(tenant_id) as conn:
        deployment = await conn.fetchrow(_DEPLOYMENT, row["deployment_id"])
    if deployment is None or deployment["status"] != DeploymentStatus.ACTIVE:
        gone = PredictionError(code=ProblemCode.NOT_ACTIVE, detail=DEPLOYMENT_GONE_DETAIL)
        return await _written(row, lambda conn: _retry_or_fail(conn, row, gone, retryable=False))
    try:
        output, elapsed_ms = await _call(row, deployment)
    except _Cancelled:
        return await _written(row, lambda conn: store.finish(conn, row, PredictionStatus.CANCELLED))
    except Problem as p:
        error, retryable = PredictionError(code=p.code, detail=p.detail), _retryable(p)
    except Exception:  # noqa: BLE001 - one prediction's bug must not stop the others; it is retried like a 5xx
        logger.exception("prediction %s failed unexpectedly", row["id"])
        error, retryable = PredictionError(code=ProblemCode.UPSTREAM_ERROR, detail=UPSTREAM_ERROR_DETAIL), True
    else:
        try:
            stored = await store.stash_output(row, output)
        except Exception:  # noqa: BLE001 - the bucket failed: retried like any other failed attempt
            logger.exception("prediction %s: its output could not be stored", row["id"])
            error, retryable = PredictionError(code=ProblemCode.UPSTREAM_ERROR, detail=UPSTREAM_ERROR_DETAIL), True
        else:
            return await _written(row, lambda conn: store.finish(
                conn, row, PredictionStatus.COMPLETED, output=stored.inline, output_ref=stored.ref, latency_ms=elapsed_ms))
    await _written(row, lambda conn: _retry_or_fail(conn, row, error, retryable))


async def _run(sem: asyncio.Semaphore, row) -> None:
    async with sem:
        try:
            await _attempt(row)
        except Exception:  # noqa: BLE001 - the lease runs out and the reaper retries it
            logger.exception("prediction %s: attempt %s could not be written back", row["id"], row["attempt"])


async def _reap(tenant_id: str) -> None:
    """Predictions whose worker died (the lease ran out) go back to the queue, or fail when they are out of attempts."""
    lost = PredictionError(code=ProblemCode.UPSTREAM_ERROR, detail=LEASE_LOST_DETAIL)
    async with TenantConn(tenant_id) as conn:
        for row in await store.expired(conn, tenant_id):
            await _retry_or_fail(conn, row, lost, retryable=True)


async def _round() -> bool:
    """Claim a share of the concurrency for each tenant with work and run what was claimed; False when nothing was due."""
    tenants = [str(r[0]) for r in await job_lifecycle.fetch(_TENANTS)]
    if not tenants:
        return False
    concurrency = settings.predict.worker_concurrency
    share = max(1, concurrency // len(tenants))
    claimed = []
    for tenant_id in tenants:
        await _reap(tenant_id)
        async with TenantConn(tenant_id) as conn:
            claimed += await store.claim(conn, tenant_id, share, WORKER_ID)
    sem = asyncio.Semaphore(concurrency)
    await asyncio.gather(*(_run(sem, row) for row in claimed))
    return bool(claimed)


async def process_predictions() -> None:
    """Poller `prediction-worker`: run the due background predictions until none is due (at most WORKER_SWEEP_MAX_ROUNDS rounds)."""
    for _ in range(WORKER_SWEEP_MAX_ROUNDS):
        if not await _round():
            return


async def purge_stored_predictions() -> None:
    """Poller `prediction-purge`: drop what retention has expired (sync idempotent rows are deleted, background ones lose input and output).
    The objects of the expiring rows go first; a row whose objects could not be deleted keeps its refs and is tried again on the next run."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        expiring = await conn.fetch(_EXPIRING_REFS)
    cleaned = []
    for r in expiring:
        try:
            await data_plane.delete_prediction_payloads(str(r["tenant_id"]), r["project_id"], r["id"])
        except Exception:  # noqa: BLE001 - one prediction's bucket error must not stop the others
            logger.exception("prediction %s: its stored payloads could not be deleted", r["id"])
        else:
            cleaned.append(r["id"])
    async with pool.acquire() as conn:
        purged = await conn.fetchval(_PURGE, cleaned)
    if purged:
        logger.info(PREDICTION_RETENTION_PURGE_LOG, purged)
