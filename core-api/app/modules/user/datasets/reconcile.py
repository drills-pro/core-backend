"""Background sweeps for datasets (plan step 8), each run on a timer from app/background.py.

reconcile_inflight_imports  polls Ray for queued/running imports and moves them to awaiting_review or failed
expire_stale_imports        expires uploads nobody started and reviews nobody finished, and purges their staging
purge_deleted_datasets      removes datasets whose soft-delete retention has passed: lakeFS repo, namespace, rows

Every import is handled in its own transaction, every step is a compare-and-set or an idempotent delete, so a
crash or a second replica repeats work harmlessly. `now` is a parameter so tests can move the clock.
"""
import logging
from datetime import UTC, datetime, timedelta

from app.core import audit
from app.core.tenant_conn import TenantConn
from app.integrations import data_plane, job_lifecycle
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
from app.modules.user.datasets.job_table import IMPORTS
from app.modules.user.datasets.lifecycle import wind_down

logger = logging.getLogger(__name__)

# committing is here for its lease: one whose lease ran out (the process died) goes back to review
_EXPIRABLE_STATUSES = (ImportStatus.AWAITING_UPLOAD, ImportStatus.AWAITING_REVIEW, ImportStatus.COMMITTING)

_EXPIRED_TENANTS = "SELECT expired_import_tenant_ids($1)"
_PURGEABLE = "SELECT tenant_id, id FROM purgeable_datasets($1)"
_EXPIRED = "SELECT * FROM dataset_imports WHERE tenant_id = $1 AND status = ANY($2) AND expires_at < $3"
_LOCK_PURGEABLE = "SELECT 1 FROM datasets WHERE id = $1 AND deleted_at < $2 FOR UPDATE"
_DATASET_IMPORTS = "SELECT id FROM dataset_imports WHERE dataset_id = $1"
_PURGE_ROWS = "SELECT purge_dataset_rows($1, $2)"


def _failure(code: IssueCode, message: str) -> ImportReport:
    return ImportReport(outcome=Outcome.FAILED, issues=[Issue(code=code, severity=Severity.ERROR, message=message)])


async def _move(row: dict, *steps: tuple[ImportStatus, dict]) -> dict | None:
    """The steps in one transaction; None when someone else moved the import first (a cancel, another replica)."""
    async with TenantConn(str(row["tenant_id"])) as conn:
        for to, cols in steps:
            row = await job_lifecycle.advance(conn, IMPORTS, row, to, **cols)
            if row is None:
                return None
    return row


async def _fail(row: dict, report: ImportReport) -> None:
    if await _move(row, (ImportStatus.FAILED, {"expires_at": None, "report": report.model_dump(mode="json")})):
        await wind_down(row, stop_job=False)  # after the commit


async def _settle(row: dict, verdict: job_lifecycle.Verdict, now: datetime) -> None:
    ids = (str(row["tenant_id"]), str(row["dataset_id"]), str(row["id"]))
    if verdict.timed_out:
        if row["ray_job_id"]:
            await job_lifecycle.stop_quietly(row["ray_job_id"])  # the job may already be gone; the import fails either way
        await _fail(row, _failure(IssueCode.JOB_TIMEOUT, "the ingest job did not finish in time"))
        return
    if verdict.ray is None or verdict.ray is JobStatus.PENDING:
        return
    to_running = [(ImportStatus.RUNNING, {})] if row["status"] == ImportStatus.QUEUED else []  # keeps the deadline
    if verdict.phase is job_lifecycle.Phase.IN_FLIGHT:
        await _move(row, *to_running)
        return
    report = await data_plane.read_report(*ids)
    if verdict.ray is JobStatus.SUCCEEDED and report is not None and report.outcome is Outcome.OK:
        review = {"expires_at": now + IMPORT_REVIEW_TTL, "report": report.model_dump(mode="json")}
        await _move(row, *to_running, (ImportStatus.AWAITING_REVIEW, review))
        return
    if report is None or report.outcome is not Outcome.FAILED:
        report = _failure(IssueCode.INTERNAL_ERROR, f"the ingest job ended as {verdict.ray} without a usable report")
    await _fail(row, report)


async def reconcile_inflight_imports(now: datetime | None = None) -> None:
    async def settle(row: dict, verdict: job_lifecycle.Verdict) -> None:
        await _settle(row, verdict, now or datetime.now(UTC))

    await job_lifecycle.sweep(IMPORTS, settle, now)


async def _expire(conn, row: dict, now: datetime) -> bool:
    """True when the import expired (its staging is to be purged); a lapsed commit lease only reopens the review."""
    if row["status"] == ImportStatus.COMMITTING:
        await job_lifecycle.advance(conn, IMPORTS, row, ImportStatus.AWAITING_REVIEW, expires_at=now + IMPORT_REVIEW_TTL)
        return False
    if await job_lifecycle.advance(conn, IMPORTS, row, ImportStatus.EXPIRED, expires_at=None) is None:
        return False  # moved by someone else meanwhile
    await audit.record(conn, tenant_id=str(row["tenant_id"]), project_id=row["project_id"], actor=SYSTEM_ACTOR,
                       action=AuditAction.IMPORT_EXPIRED, entity_type=EntityType.DATASET_IMPORT, entity_id=row["id"])
    return True


async def expire_stale_imports(now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    for tenant_id in [r[0] for r in await job_lifecycle.fetch(_EXPIRED_TENANTS, now)]:
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
    for tenant_id, dataset_id in [(r[0], r[1]) for r in await job_lifecycle.fetch(_PURGEABLE, cutoff)]:
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
