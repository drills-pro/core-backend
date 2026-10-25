"""Background sweeps for datasets (plan step 8), each run on a timer from app/background.py.

reconcile_inflight_imports  polls Ray for queued/running imports and moves them to awaiting_review or failed
expire_stale_imports        expires uploads nobody started and reviews nobody finished, and purges their staging
purge_deleted_datasets      removes datasets whose soft-delete retention has passed: lakeFS repo, namespace, rows

Every import is handled in its own transaction, every step is a compare-and-set or an idempotent delete, so a
crash or a second replica repeats work harmlessly. `now` is a parameter so tests can move the clock.
"""
import logging
import uuid
from datetime import UTC, datetime, timedelta

from app.core import audit
from app.core.db import get_pool
from app.core.tenant_conn import TenantConn
from app.integrations import compute, data_plane, job_follower
from app.integrations.compute import JobStatus
from app.integrations.data_plane.report import ImportReport, Issue, IssueCode, Outcome, Severity
from app.modules.user.datasets.constants import (
    DATASET_RETENTION_DAYS,
    IMPORT_REVIEW_TTL,
    SYSTEM_ACTOR,
    AuditAction,
    EntityType,
    ImportStatus,
)
from app.modules.user.datasets.import_service import cas
from app.modules.user.datasets.lifecycle import wind_down

logger = logging.getLogger(__name__)

_RUNNING_STATUSES = (ImportStatus.QUEUED, ImportStatus.RUNNING)
# committing is here for its lease: one whose lease ran out (the process died) goes back to review
_EXPIRABLE_STATUSES = (ImportStatus.AWAITING_UPLOAD, ImportStatus.AWAITING_REVIEW, ImportStatus.COMMITTING)

_RUNNING_TENANTS = "SELECT inflight_import_tenant_ids()"
_EXPIRED_TENANTS = "SELECT expired_import_tenant_ids($1)"
_PURGEABLE = "SELECT tenant_id, id FROM purgeable_datasets($1)"
_RUNNING = "SELECT * FROM dataset_imports WHERE tenant_id = $1 AND status = ANY($2)"
_EXPIRED = "SELECT * FROM dataset_imports WHERE tenant_id = $1 AND status = ANY($2) AND expires_at < $3"
_STORE_REPORT = "UPDATE dataset_imports SET report = $2 WHERE id = $1"
_LOCK_PURGEABLE = "SELECT 1 FROM datasets WHERE id = $1 AND deleted_at < $2 FOR UPDATE"
_DATASET_IMPORTS = "SELECT id FROM dataset_imports WHERE dataset_id = $1"
_PURGE_ROWS = "SELECT purge_dataset_rows($1, $2)"


async def _fetch(sql: str, *args) -> list:
    """A cross-tenant listing (SECURITY DEFINER function), before any tenant is set."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(sql, *args)


def _failure(code: IssueCode, message: str) -> ImportReport:
    return ImportReport(outcome=Outcome.FAILED, issues=[Issue(code=code, severity=Severity.ERROR, message=message)])


async def _fail(conn, row: dict, report: ImportReport) -> None:
    await cas(conn, row, ImportStatus.FAILED)
    await conn.execute(_STORE_REPORT, row["id"], report.model_dump(mode="json"))
    await wind_down(row, stop_job=False)


async def _ray_status(row: dict) -> JobStatus | None:
    """Ray's state of the import's job; None when Ray does not answer or answers something unknown (the job was
    garbage-collected, the head restarted): the deadline below must still be able to end such an import."""
    if not row["ray_job_id"]:
        return JobStatus.PENDING
    return await job_follower.poll(row["ray_job_id"])


async def _settle(conn, row: dict, now: datetime) -> None:
    ids = (str(row["tenant_id"]), str(row["dataset_id"]), str(row["id"]))
    ray = await _ray_status(row)
    done = ray is not None and job_follower.phase(ray) is not job_follower.Phase.IN_FLIGHT
    if not done and row["expires_at"] is not None and row["expires_at"] <= now:  # the job deadline
        if row["ray_job_id"]:
            try:
                await compute.stop(row["ray_job_id"])
            except Exception:  # noqa: BLE001 - the job may already be gone; the import fails either way
                logger.warning("could not stop timed-out ingest job %s", row["ray_job_id"])
        await _fail(conn, row, _failure(IssueCode.JOB_TIMEOUT, "the ingest job did not finish in time"))
        return
    if ray is None or ray is JobStatus.PENDING:
        return
    if row["status"] == ImportStatus.QUEUED:
        row = await cas(conn, row, ImportStatus.RUNNING, row["expires_at"])  # keep the deadline
    if not done:
        return
    report = await data_plane.read_report(*ids)
    if ray is JobStatus.SUCCEEDED and report is not None and report.outcome is Outcome.OK:
        await cas(conn, row, ImportStatus.AWAITING_REVIEW, now + IMPORT_REVIEW_TTL)
        await conn.execute(_STORE_REPORT, row["id"], report.model_dump(mode="json"))
        return
    if report is None or report.outcome is not Outcome.FAILED:
        report = _failure(IssueCode.INTERNAL_ERROR, f"the ingest job ended as {ray} without a usable report")
    await _fail(conn, row, report)


async def reconcile_inflight_imports(now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)

    async def select(conn, tenant_id):
        return await conn.fetch(_RUNNING, tenant_id, [str(s) for s in _RUNNING_STATUSES])

    async def settle(conn, row):
        await _settle(conn, row, now)

    await job_follower.follow(_RUNNING_TENANTS, select, settle)


async def _expire(conn, row: dict, now: datetime) -> bool:
    """True when the import expired (its staging is to be purged); a lapsed commit lease only reopens the review."""
    if row["status"] == ImportStatus.COMMITTING:
        await cas(conn, row, ImportStatus.AWAITING_REVIEW, now + IMPORT_REVIEW_TTL)
        return False
    await cas(conn, row, ImportStatus.EXPIRED)
    await audit.record(conn, tenant_id=str(row["tenant_id"]), project_id=row["project_id"], actor=SYSTEM_ACTOR,
                       action=AuditAction.IMPORT_EXPIRED, entity_type=EntityType.DATASET_IMPORT, entity_id=row["id"])
    return True


async def expire_stale_imports(now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    for tenant_id in [r[0] for r in await _fetch(_EXPIRED_TENANTS, now)]:
        async with TenantConn(str(tenant_id)) as conn:
            rows = [dict(r) for r in await conn.fetch(_EXPIRED, tenant_id, [str(s) for s in _EXPIRABLE_STATUSES], now)]
        for row in rows:
            try:
                async with TenantConn(str(tenant_id)) as conn:
                    expired = await _expire(conn, row, now)
                if expired:
                    await wind_down(row, stop_job=False)  # after the commit
            except Exception:  # noqa: BLE001 - a import moved by someone else (409) or a store hiccup retries next tick
                logger.exception("failed to expire import %s", row["id"])


async def purge_deleted_datasets(now: datetime | None = None) -> None:
    """Delete datasets soft-deleted more than DATASET_RETENTION_DAYS ago. The dataset row stays locked from the
    check until the rows are gone, so a restore cannot slip in between the repo delete and the row delete."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=DATASET_RETENTION_DAYS)
    for tenant_id, dataset_id in [(r[0], r[1]) for r in await _fetch(_PURGEABLE, cutoff)]:
        try:
            async with TenantConn(str(tenant_id)) as conn:
                if await conn.fetchval(_LOCK_PURGEABLE, dataset_id, cutoff) is None:
                    continue  # restored, or purged by another replica
                await data_plane.delete_dataset_repo(str(tenant_id), str(dataset_id))  # already gone is fine
                for imp in await conn.fetch(_DATASET_IMPORTS, dataset_id):  # staging lives outside the repo
                    await data_plane.purge_staging(str(tenant_id), str(dataset_id), str(imp["id"]))
                await conn.fetchval(_PURGE_ROWS, dataset_id, cutoff)
        except Exception:  # noqa: BLE001 - retried on the next tick
            logger.exception("failed to purge dataset %s", dataset_id)
