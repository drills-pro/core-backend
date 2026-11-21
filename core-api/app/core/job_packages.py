"""The job code the platform runs on Ray, one `job_packages` row per (name, content hash): where its zip is staged and
whether runs may use it (candidate -> stable -> retired). Platform data, written only through /v1/admin/job-packages.
"""
import logging
from datetime import datetime
from http import HTTPStatus

from pydantic import BaseModel

from app.core.db import get_pool
from app.core.enums import JobPackageName, PackageAuditAction, PackageKind, PackageStatus, SmokeStatus
from app.core.problem import Problem
from app.core.requirements import RequirementsError, merge_requirements
from app.core.settings import settings

logger = logging.getLogger(__name__)

REQUIREMENTS_CONFLICT = "requirements_conflict"  # two packages of one run pin the same distribution differently

TASK_PACKAGE_PREFIX = "task_"
TASK_PACKAGE_PATTERN = r"^task_[a-z][a-z0-9-]*$"  # mirrors the task half of job_packages_name_check


def task_package_name(slug: str) -> str:
    """The job_packages name of the bundle that trains `slug`."""
    return f"{TASK_PACKAGE_PREFIX}{slug}"


class JobPackage(BaseModel):
    name: str  # a JobPackageName for a runtime package, task_<slug> for a task bundle
    kind: PackageKind = PackageKind.RUNTIME
    manifest: dict | None = None  # the bundle's manifest.json; None for a runtime package
    requirements: list[str] = []  # exact pins Ray installs for this package's jobs (planning/26); empty = the image's own packages
    sha256: str
    s3_key: str
    status: PackageStatus
    published_by: str
    updated_by: str
    updated_at: datetime
    smoke_status: SmokeStatus
    smoke_job_id: str | None


def merged_requirements(*packages: JobPackage) -> list[str]:
    """What Ray installs for a run on these packages together (the core and its bundle, or a package alone): the union of
    their pins, sorted. The same pin in two packages merges; two different pins for one distribution are a 409."""
    try:
        return merge_requirements(*(p.requirements for p in packages))
    except RequirementsError as e:
        raise Problem(HTTPStatus.CONFLICT, REQUIREMENTS_CONFLICT, f"{' + '.join(p.name for p in packages)}: {e}") from None


_SELECT = "SELECT * FROM job_packages"
_INSERT = (
    "INSERT INTO job_packages (name, sha256, s3_key, published_by, updated_by, kind, manifest, requirements) VALUES ($1, $2, $3, $4, $4, $5, $6, $7) "
    "ON CONFLICT (name, sha256) DO NOTHING RETURNING true"
)
_SET_STATUS = "UPDATE job_packages SET status = $3, updated_by = $4, updated_at = now() WHERE name = $1 AND sha256 = $2 RETURNING *"

_RESET_FAILED_SMOKE = (
    "UPDATE job_packages SET smoke_status = $5, smoke_job_id = NULL WHERE name = $1 AND sha256 = $2 AND status = $3 AND smoke_status = $4 RETURNING *"
)
_AUDIT = "INSERT INTO job_package_audit (name, sha256, action, actor) VALUES ($1, $2, $3, $4)"


async def _record(conn, name: str, sha256: str, action: PackageAuditAction, actor: str) -> None:
    """Append one job_package_audit row, in the transaction of the change it records."""
    await conn.execute(_AUDIT, name, sha256, action, actor)


async def register(
    conn, name: str, sha256: str, s3_key: str, actor: str, requirements: list[str] | None = None, manifest: dict | None = None,
) -> JobPackage:
    """A candidate for this content, audited when new; the row that already exists for it otherwise. A `manifest` makes it a
    task bundle (`name` is task_<slug>), none a runtime package."""
    kind = PackageKind.RUNTIME if manifest is None else PackageKind.TASK
    async with conn.transaction():
        if await conn.fetchval(_INSERT, name, sha256, s3_key, actor, kind, manifest, requirements or []):
            await _record(conn, name, sha256, PackageAuditAction.REGISTER, actor)
        return JobPackage.model_validate(dict(await conn.fetchrow(f"{_SELECT} WHERE name = $1 AND sha256 = $2", name, sha256)))


async def get(conn, name: JobPackageName, sha256: str) -> JobPackage | None:
    row = await conn.fetchrow(f"{_SELECT} WHERE name = $1 AND sha256 = $2", name, sha256)
    return None if row is None else JobPackage.model_validate(dict(row))


async def latest_stable(conn, name: JobPackageName) -> JobPackage | None:
    row = await conn.fetchrow(f"{_SELECT} WHERE name = $1 AND status = $2 ORDER BY updated_at DESC LIMIT 1", name, PackageStatus.STABLE)
    return None if row is None else JobPackage.model_validate(dict(row))


async def list_for(conn, name: JobPackageName) -> list[JobPackage]:
    return [JobPackage.model_validate(dict(r)) for r in await conn.fetch(f"{_SELECT} WHERE name = $1 ORDER BY updated_at DESC", name)]


async def list_kind(conn, kind: PackageKind, status: PackageStatus | None = None) -> list[JobPackage]:
    """Every package of `kind` (in `status` when given), oldest change first."""
    rows = await conn.fetch(f"{_SELECT} WHERE kind = $1 AND ($2::text IS NULL OR status = $2) ORDER BY name, updated_at", kind, status)
    return [JobPackage.model_validate(dict(r)) for r in rows]


async def set_status(conn, name: JobPackageName, sha256: str, status: PackageStatus, actor: str) -> JobPackage | None:
    """The bare status write, unaudited and with no one-stable rule: `promote` and `retire` are the lifecycle's moves."""
    row = await conn.fetchrow(_SET_STATUS, name, sha256, status, actor)
    return None if row is None else JobPackage.model_validate(dict(row))


async def promote(conn, name: str, sha256: str, actor: str) -> JobPackage | None:
    """Make `sha256` the one stable package of `name`, audited: the previous stable is retired in the same transaction.
    None when there is no such package."""
    async with conn.transaction():
        if await get(conn, name, sha256) is None:  # retire the old stable only for a package that exists
            return None
        await conn.execute(
            "UPDATE job_packages SET status = $4, updated_by = $5, updated_at = now() WHERE name = $1 AND status = $2 AND sha256 <> $3",
            name, PackageStatus.STABLE, sha256, PackageStatus.RETIRED, actor)
        promoted = await set_status(conn, name, sha256, PackageStatus.STABLE, actor)
        if promoted is not None:
            await _record(conn, name, sha256, PackageAuditAction.PROMOTE, actor)
        return promoted


async def retire(conn, name: str, sha256: str, actor: str, status: PackageStatus = PackageStatus.RETIRED) -> JobPackage | None:
    """Take a package out of use (`status` retired, or back to candidate), audited. None when there is no such package."""
    async with conn.transaction():
        retired = await set_status(conn, name, sha256, status, actor)
        if retired is not None:
            await _record(conn, name, sha256, PackageAuditAction.RETIRE, actor)
        return retired


async def start_smoke(conn, name: JobPackageName, sha256: str, job_id: str) -> None:
    await conn.execute("UPDATE job_packages SET smoke_job_id = $3 WHERE name = $1 AND sha256 = $2 AND smoke_job_id IS NULL", name, sha256, job_id)


async def set_smoke(conn, name: JobPackageName, sha256: str, smoke: SmokeStatus) -> JobPackage:
    row = await conn.fetchrow("UPDATE job_packages SET smoke_status = $3 WHERE name = $1 AND sha256 = $2 RETURNING *", name, sha256, smoke)
    return JobPackage.model_validate(dict(row))


async def reset_failed_smoke(conn, name: JobPackageName, sha256: str) -> JobPackage | None:
    """Compare-and-set: a candidate whose smoke failed goes back to pending with no job. None when it is not in that state
    (unknown, promoted or retired, or another retry got there first), so concurrent retries start one job."""
    row = await conn.fetchrow(_RESET_FAILED_SMOKE, name, sha256, PackageStatus.CANDIDATE, SmokeStatus.FAILED, SmokeStatus.PENDING)
    return None if row is None else JobPackage.model_validate(dict(row))


async def purge_audit() -> None:
    """Poller `job-packages-audit-purge`: keep the newest `job_packages.audit_keep_per_name` rows of each package name."""
    async with (await get_pool()).acquire() as conn:
        dropped = await conn.fetchval("SELECT purge_job_package_audit($1)", settings.job_packages.audit_keep_per_name)
    if dropped:
        logger.info("purged %s job package audit rows", dropped)
