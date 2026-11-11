"""The import lifecycle: create, start, get, list, cancel, commit, discard.

Every status change is a compare-and-set (`UPDATE ... WHERE status = <from>`) along an edge of
constants.TRANSITIONS, so a retry or a race loses cleanly with 409 instead of acting twice. Commit also takes
a row lock on the dataset, which serialises version numbering and the stale-base check.

No pooled connection is held across a gateway call (lakeFS, staging, Ray): the gateway checks ownership on a
connection of its own, so holding one while waiting for it could exhaust the pool. Each step is therefore
short transactions around the call. A commit is claimed by moving the import to `committing` (one per
dataset) and finished by a second transaction; the lakeFS commit carries the import id, so resuming a
commit that failed in between returns the commit already made instead of making another.
"""
import logging
import uuid
from datetime import UTC, datetime, timedelta
from http import HTTPStatus


from app.core import audit, plan_limits, task_types
from app.core.authz import Action, AuthzContext
from app.core.enums import TaskType
from app.core.idempotency import insert_once, replay
from app.core.metrics import usage
from app.core.pagination import Page, PageParams, SortDir, build_page, keyset_clause, order_by
from app.core.problem import Problem, not_found
from app.core.settings import settings
from app.core.tenant_conn import TenantConn
from app.integrations import data_plane, job_lifecycle
from app.integrations.data_plane import layout
from app.integrations.data_plane.report import ImportReport
from app.modules.user.datasets.constants import (
    ACTIVE_STATUSES,
    BASE_REQUIRED_DETAIL,
    COMMIT_LEASE_GRACE,
    IMPORT_REVIEW_TTL,
    IMPORT_UPLOAD_TTL,
    START_FAILED_DETAIL,
    STALE_BASE_DETAIL,
    AuditAction,
    EntityType,
    ImportSource,
    ImportStatus,
    ProblemCode,
)
from app.modules.user.datasets import ingest_spec, pipeline_log
from app.modules.user.datasets.job_table import IMPORTS
from app.modules.user.datasets.lifecycle import purge_staging_quietly, wind_down
from app.modules.user.datasets.readers import dataset_task
from app.modules.user.datasets.schemas import DatasetImport, DatasetVersion, ImportCreate

logger = logging.getLogger(__name__)


_LOCK_DATASET = "SELECT id FROM datasets WHERE project_id = $1 AND id = $2 AND deleted_at IS NULL FOR UPDATE"
_IMPORT = (
    "SELECT i.* FROM dataset_imports i JOIN datasets d ON d.id = i.dataset_id "
    "WHERE i.id = $1 AND i.dataset_id = $2 AND d.project_id = $3 AND d.deleted_at IS NULL"
)
_BY_KEY = "SELECT * FROM dataset_imports WHERE dataset_id = $1 AND idempotency_key = $2"
_BASE_EXISTS = "SELECT 1 FROM dataset_versions WHERE id = $1 AND dataset_id = $2"
_LATEST_VERSION = "SELECT id FROM dataset_versions WHERE dataset_id = $1 ORDER BY number DESC LIMIT 1"
_TENANT_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))"  # serialises the cap check and insert per tenant
_ACTIVE_COUNT = "SELECT count(*) FROM dataset_imports WHERE tenant_id = $1 AND status = ANY($2)"
_INSERT = (
    "INSERT INTO dataset_imports (id, tenant_id, project_id, dataset_id, source, base_version_id, params, "
    "staging_key, expires_at, idempotency_key) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) "
    "ON CONFLICT (dataset_id, idempotency_key) DO NOTHING RETURNING *"
)
_SET_JOB = "UPDATE dataset_imports SET ray_job_id = $2 WHERE id = $1 AND status = $3 RETURNING id"  # a cancel in between wins
# While `committing`, expires_at is the lease: the commit belongs to whoever set it until it passes.
_OTHER_COMMITTING = "SELECT * FROM dataset_imports WHERE dataset_id = $1 AND id <> $2 AND status = $3 LIMIT 1"
_RESUME_COMMIT = "UPDATE dataset_imports SET expires_at = $2 WHERE id = $1 AND status = $3 AND expires_at <= $4 RETURNING *"
_RELEASE_COMMIT = "UPDATE dataset_imports SET expires_at = $2 WHERE id = $1 AND status = $3"
_RUNNING_OF_DATASET = "SELECT * FROM dataset_imports WHERE dataset_id = $1 AND status = ANY($2)"
_NEW_VERSION = (
    "INSERT INTO dataset_versions (tenant_id, dataset_id, number, lakefs_commit, import_id, stats) "
    "SELECT $1, $2, COALESCE(max(number), 0) + 1, $3, $4, $5 FROM dataset_versions WHERE dataset_id = $2 "
    "RETURNING id, number, import_id, stats, created_at"
)
_VERSION_OF_IMPORT = "SELECT id, number, import_id, stats, created_at FROM dataset_versions WHERE import_id = $1"
_LIST = "SELECT * FROM dataset_imports WHERE dataset_id = $1 {cond} ORDER BY {order} LIMIT {limit}"
_DATASET_EXISTS = "SELECT 1 FROM datasets WHERE project_id = $1 AND id = $2 AND deleted_at IS NULL"


async def _load(conn, project_id, dataset_id, import_id) -> dict:
    row = await conn.fetchrow(_IMPORT, import_id, dataset_id, project_id)
    if row is None:  # RLS hides other tenants' rows: indistinguishable from a missing one
        raise not_found()
    return dict(row)


async def cas(conn, row: dict, to: ImportStatus, expires_at: datetime | None = None) -> dict:
    """Move the import from the status it was read at to `to`; 409 when it is not an edge or someone moved it first."""
    moved = await job_lifecycle.advance(conn, IMPORTS, row, to, expires_at=expires_at)
    if moved is None:
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.INVALID_STATE, f"import is {ImportStatus(row['status'])}, it cannot become {to}")
    return moved


async def _check_base(conn, row: dict) -> None:
    """An import applies on top of the latest version. One with no base is only for a dataset with no versions: it
    would rewrite the index and data files whole, and there is no overwrite mode."""
    if await conn.fetchval(_LATEST_VERSION, row["dataset_id"]) != row["base_version_id"]:
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.STALE_BASE, STALE_BASE_DETAIL)


async def _validate(conn, task_type: TaskType, body: ImportCreate) -> None:
    info = await task_types.require_supported(conn, task_type)
    if body.task_type is not None and body.task_type != task_type:
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.TASK_TYPE_MISMATCH, f"this project's task type is '{task_type}'")
    if body.source is ImportSource.HUGGINGFACE and body.params.repo is None:
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.INVALID_PARAMS, "a Hugging Face import needs params.repo")
    if body.source is ImportSource.HUGGINGFACE and task_type == TaskType.TEXT_CLASSIFICATION:
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.INVALID_PARAMS, "a text dataset is imported from a CSV upload for now")
    if body.source is ImportSource.UPLOAD and info.is_tabular and body.params.target is None:
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.INVALID_PARAMS, "a CSV import needs params.target")


def _same_request(row, body: ImportCreate) -> bool:
    return (
        row["source"] == body.source
        and row["base_version_id"] == body.base_version_id
        and row["params"] == body.params.model_dump(mode="json", exclude_none=True)
    )


async def _to_model(ctx: AuthzContext, row: dict, *, with_upload_url: bool = False) -> DatasetImport:
    model = DatasetImport.model_validate(row)
    if with_upload_url and model.source is ImportSource.UPLOAD and model.status is ImportStatus.AWAITING_UPLOAD:
        model.upload_url = await data_plane.presign_staging_put(ctx.tenant_id, str(model.dataset_id), str(model.id))
    return model


async def create_import(
    ctx: AuthzContext, project_id: uuid.UUID, dataset_id: uuid.UUID, body: ImportCreate, idempotency_key: str
) -> DatasetImport:
    async with TenantConn(ctx.tenant_id) as conn:
        task = await dataset_task(conn, project_id, dataset_id)  # 404 for a dataset that is not a live one of the project
        row = await conn.fetchrow(_BY_KEY, dataset_id, idempotency_key)
        created = row is None
        if created:
            if task is None:
                raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.TASK_TYPE_MISSING, "this project has no task type, so it cannot import data")
            await _validate(conn, TaskType(task), body)
            if body.base_version_id is None and await conn.fetchval(_LATEST_VERSION, dataset_id) is not None:
                raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.BASE_REQUIRED, BASE_REQUIRED_DETAIL)
            if body.base_version_id and await conn.fetchval(_BASE_EXISTS, body.base_version_id, dataset_id) is None:
                raise not_found()
            await conn.fetchval(_TENANT_LOCK, ctx.tenant_id)  # a concurrent create waits here, then sees this one
            active = await conn.fetchval(_ACTIVE_COUNT, ctx.tenant_id, [str(s) for s in ACTIVE_STATUSES])
            if active >= (await plan_limits.dataset_limits(conn, ctx.tenant_id)).max_active_imports_per_tenant:
                raise Problem(
                    HTTPStatus.TOO_MANY_REQUESTS, ProblemCode.TOO_MANY_IMPORTS, "too many imports in progress, wait for one to finish"
                )
            import_id = uuid.uuid4()
            row, created = await insert_once(
                conn, _INSERT,
                (import_id, ctx.tenant_id, project_id, dataset_id, body.source, body.base_version_id,
                 body.params.model_dump(mode="json", exclude_none=True),
                 layout.staging_prefix(ctx.tenant_id, str(dataset_id), str(import_id)),
                 datetime.now(UTC) + IMPORT_UPLOAD_TTL, idempotency_key),
                _BY_KEY, (dataset_id, idempotency_key),
            )
            if created:
                await audit.record_for(conn, ctx, project_id, AuditAction.IMPORT_CREATED, EntityType.DATASET_IMPORT, import_id)
    if not created:
        replay(row, lambda r: _same_request(r, body), ProblemCode.IDEMPOTENCY_MISMATCH)
    if created:
        usage.dataset_imports.add(1, {"source": body.source})
    return await _to_model(ctx, dict(row), with_upload_url=True)


async def get_import(ctx: AuthzContext, project_id: uuid.UUID, dataset_id: uuid.UUID, import_id: uuid.UUID) -> DatasetImport:
    async with TenantConn(ctx.tenant_id) as conn:
        row = await _load(conn, project_id, dataset_id, import_id)
    # the upload target is a write capability: a reader who could PUT would feed bytes to the import an editor starts
    return await _to_model(ctx, row, with_upload_url=Action.DATASET_WRITE in ctx.actions)


async def list_imports(ctx: AuthzContext, project_id: uuid.UUID, dataset_id: uuid.UUID, page: PageParams) -> Page[DatasetImport]:
    async with TenantConn(ctx.tenant_id) as conn:
        if await conn.fetchval(_DATASET_EXISTS, project_id, dataset_id) is None:
            raise not_found()
        cond, args = keyset_clause(page.page_token, SortDir.DESC, first_param=2)
        rows = await conn.fetch(
            _LIST.format(cond=cond, order=order_by(SortDir.DESC), limit=page.page_size + 1), dataset_id, *args
        )
    return build_page(rows, page.page_size, DatasetImport)


async def _unqueue(ctx: AuthzContext, row: dict) -> None:
    """Put a start that failed back to awaiting_upload so it can be retried. If this fails too, the import stays
    queued with no job and the reconciler fails it at its deadline."""
    try:
        async with TenantConn(ctx.tenant_id) as conn:
            await cas(conn, row, ImportStatus.AWAITING_UPLOAD, datetime.now(UTC) + IMPORT_UPLOAD_TTL)
    except Exception:  # noqa: BLE001 - see above
        logger.exception("could not return import %s to awaiting_upload", row["id"])


def _start_failed(e: Exception, imp: str) -> Exception:
    """What the caller sees when starting failed: a Problem stays as it is, anything else (storage, Ray, the
    database) becomes a 503 the frontend can show, with the cause in the log."""
    if isinstance(e, Problem):
        return e
    logger.error("could not start import %s", imp, exc_info=e)
    problem = Problem(HTTPStatus.SERVICE_UNAVAILABLE, ProblemCode.START_FAILED, START_FAILED_DETAIL)
    problem.__cause__ = e
    return problem


async def start_import(ctx: AuthzContext, project_id: uuid.UUID, dataset_id: uuid.UUID, import_id: uuid.UUID) -> DatasetImport:
    tenant, ds, imp = ctx.tenant_id, str(dataset_id), str(import_id)
    async with TenantConn(ctx.tenant_id) as conn:
        row = await _load(conn, project_id, dataset_id, import_id)
        task_type = await dataset_task(conn, project_id, dataset_id)
        info = await task_types.require_supported(conn, TaskType(task_type))
        await _check_base(conn, row)
        # expires_at is the job deadline while queued/running, so the timeout runs from the start, not from creation
        deadline = datetime.now(UTC) + timedelta(seconds=settings.dataset_limits.job_timeout_seconds)
        limits = await plan_limits.dataset_limits(conn, tenant)
        row = await cas(conn, row, ImportStatus.QUEUED, deadline)
    submission_id = f"ingest-{imp}-{uuid.uuid4().hex[:8]}"  # Ray refuses a reused id, and a retry is a new submission; known before the POST, so a lost answer can still be stopped
    try:
        await data_plane.create_dataset_repo(tenant, ds)
        spec, kind = await ingest_spec.build(row, TaskType(task_type), info.data_kind, limits, submission_id)
        job_id = await data_plane.submit_ingest(tenant, ds, imp, spec, kind)
    except Exception as e:
        await job_lifecycle.stop_quietly(submission_id)  # Ray may have accepted it: a retry must not run a second job on this prefix
        await _unqueue(ctx, row)
        raise _start_failed(e, imp)
    try:
        async with TenantConn(ctx.tenant_id) as conn:
            tracked = await conn.fetchval(_SET_JOB, import_id, job_id, ImportStatus.QUEUED)
    except Exception as e:
        await job_lifecycle.stop_quietly(job_id)  # nothing would track it
        await _unqueue(ctx, row)
        raise _start_failed(e, imp)
    if tracked is None:  # cancelled or deleted meanwhile, when there was no job id yet to stop
        await job_lifecycle.stop_quietly(job_id)
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.INVALID_STATE, "the import was cancelled while it was starting")
    row["ray_job_id"] = job_id
    pipeline_log.started(row)
    return await _to_model(ctx, row)


async def cancel_import(ctx: AuthzContext, project_id: uuid.UUID, dataset_id: uuid.UUID, import_id: uuid.UUID) -> DatasetImport:
    async with TenantConn(ctx.tenant_id) as conn:
        row = await _load(conn, project_id, dataset_id, import_id)
        moved = await cas(conn, row, ImportStatus.CANCELLED)
    await wind_down(row)
    return await _to_model(ctx, moved)


async def cancel_running_imports(conn, dataset_id: uuid.UUID) -> list[dict]:
    """Cancel the queued and running imports of a dataset that is being deleted; the caller then calls
    finish_cancelled once the transaction has committed."""
    rows = await conn.fetch(_RUNNING_OF_DATASET, dataset_id, [str(ImportStatus.QUEUED), str(ImportStatus.RUNNING)])
    return [await cas(conn, dict(r), ImportStatus.CANCELLED) for r in rows]


async def finish_cancelled(cancelled: list[dict]) -> None:
    for row in cancelled:
        await wind_down(row)


async def discard_import(ctx: AuthzContext, project_id: uuid.UUID, dataset_id: uuid.UUID, import_id: uuid.UUID) -> DatasetImport:
    async with TenantConn(ctx.tenant_id) as conn:
        row = await cas(conn, await _load(conn, project_id, dataset_id, import_id), ImportStatus.DISCARDED)
        await audit.record_for(conn, ctx, project_id, AuditAction.IMPORT_DISCARDED, EntityType.DATASET_IMPORT, import_id)
    await purge_staging_quietly(ctx.tenant_id, str(dataset_id), str(import_id))
    return await _to_model(ctx, row)


async def _version_of(conn, import_id: uuid.UUID) -> DatasetVersion:
    return DatasetVersion.model_validate(dict(await conn.fetchrow(_VERSION_OF_IMPORT, import_id)))


async def _claim_commit(ctx: AuthzContext, project_id, dataset_id, import_id) -> dict | DatasetVersion:
    """Move the import to `committing` (or take over one whose lease ran out); an import that already succeeded
    answers with its version. Only one import of a dataset is committing at a time."""
    async with TenantConn(ctx.tenant_id) as conn:
        if await conn.fetchval(_LOCK_DATASET, project_id, dataset_id) is None:
            raise not_found()
        row = await _load(conn, project_id, dataset_id, import_id)  # read under the lock, so a concurrent claim is visible
        if row["status"] == ImportStatus.SUCCEEDED:
            return await _version_of(conn, import_id)
        now = datetime.now(UTC)
        lease = now + timedelta(seconds=settings.data_plane.commit_timeout_seconds) + COMMIT_LEASE_GRACE
        if row["status"] == ImportStatus.COMMITTING:
            resumed = await conn.fetchrow(_RESUME_COMMIT, import_id, lease, ImportStatus.COMMITTING, now)
            if resumed is None:
                raise Problem(HTTPStatus.CONFLICT, ProblemCode.INVALID_STATE, "this import is being committed, try again shortly")
            return dict(resumed)  # nothing else could commit meanwhile, so the base needs no second check
        other = await conn.fetchrow(_OTHER_COMMITTING, dataset_id, import_id, ImportStatus.COMMITTING)
        if other is not None and other["expires_at"] <= now:  # its process died: reopen its review, so it cannot block this one
            await cas(conn, dict(other), ImportStatus.AWAITING_REVIEW, now + IMPORT_REVIEW_TTL)
            other = None
        if other is not None:
            if row["base_version_id"] is not None and other["base_version_id"] == row["base_version_id"]:
                raise Problem(HTTPStatus.CONFLICT, ProblemCode.STALE_BASE, STALE_BASE_DETAIL)  # it will be the latest in a moment
            raise Problem(HTTPStatus.CONFLICT, ProblemCode.INVALID_STATE, "another import of this dataset is being committed")
        await _check_base(conn, row)
        return await cas(conn, row, ImportStatus.COMMITTING, lease)


async def _release_commit(ctx: AuthzContext, import_id: uuid.UUID) -> None:
    """A commit that failed here can be resumed at once, not after its lease."""
    try:
        async with TenantConn(ctx.tenant_id) as conn:
            await conn.execute(_RELEASE_COMMIT, import_id, datetime.now(UTC), ImportStatus.COMMITTING)
    except Exception:  # noqa: BLE001 - the lease runs out on its own
        logger.exception("could not release the commit lease of import %s", import_id)


async def commit_import(ctx: AuthzContext, project_id: uuid.UUID, dataset_id: uuid.UUID, import_id: uuid.UUID) -> DatasetVersion:
    """Turn a reviewed import into the dataset's next version. Committing again returns the same version."""
    claimed = await _claim_commit(ctx, project_id, dataset_id, import_id)
    if isinstance(claimed, DatasetVersion):
        return claimed
    try:
        async with TenantConn(ctx.tenant_id) as conn:
            limits = await plan_limits.dataset_limits(conn, ctx.tenant_id)
        commit = await data_plane.commit_staged(
            ctx.tenant_id, str(dataset_id), str(import_id), f"Import {import_id}", limits
        )
        async with TenantConn(ctx.tenant_id) as conn:
            if await conn.fetchval(_LOCK_DATASET, project_id, dataset_id) is None:
                raise not_found()
            row = await _load(conn, project_id, dataset_id, import_id)
            if row["status"] == ImportStatus.SUCCEEDED:  # a resumed commit finished first
                return await _version_of(conn, import_id)
            stats = ImportReport.model_validate(row["report"]).stats
            version = await conn.fetchrow(
                _NEW_VERSION, ctx.tenant_id, dataset_id, commit, import_id, stats.model_dump(mode="json")
            )
            await cas(conn, row, ImportStatus.SUCCEEDED)
            await audit.record_for(conn, ctx, project_id, AuditAction.IMPORT_COMMITTED, EntityType.DATASET_IMPORT, import_id)
    except Exception as e:
        if not isinstance(e, Problem):  # a Problem is the caller's (the dataset went away), not the store's
            pipeline_log.storage_failed(claimed, e)
        await _release_commit(ctx, import_id)  # so the retry resumes now; lakeFS answers it with the commit already made
        raise
    usage.dataset_import_bytes.add(stats.total_bytes, {"source": row["source"]})
    await purge_staging_quietly(ctx.tenant_id, str(dataset_id), str(import_id))
    return DatasetVersion.model_validate(dict(version))
