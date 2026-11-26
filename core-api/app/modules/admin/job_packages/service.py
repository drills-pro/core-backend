"""The job packages' moves: publish or register a version, and make it a package's target once its smoke passed. CI calls
`target` for every new version, and the poller `promote_targets` moves targets that became eligible. The rules (smoke gate,
one target per name, the core x bundle re-check, audit) are the lifecycle's (data_plane/job_package.py, core/job_packages.py);
each write here is one transaction. A gateway call (Ray, S3) never runs inside a pooled connection, so a move reads the row,
settles its smoke, then writes in a connection of its own."""
import logging
from http import HTTPStatus

from app.core import job_packages
from app.core.db import get_pool
from app.core.enums import JobPackageName, PackageKind, PackageStatus, SmokeStatus, TaskType
from app.core.job_packages import TASK_PACKAGE_PREFIX, JobPackage
from app.core.problem import Problem, not_found
from app.integrations import data_plane
from app.modules.admin import task_bundles
from app.modules.admin.job_packages.constants import (
    AUTO_PROMOTER,
    SMOKE_NOT_PASSED_DETAIL,
    SMOKE_NOT_RETRYABLE_DETAIL,
    STABLE_NOT_RETIRABLE_DETAIL,
    ProblemCode,
)

logger = logging.getLogger(__name__)


async def list_packages(name: JobPackageName) -> list[JobPackage]:
    async with (await get_pool()).acquire() as conn:
        packages = await job_packages.list_for(conn, name)
    return [await data_plane.refresh_package_smoke(p) for p in packages]


async def publish(name: JobPackageName, actor: str) -> JobPackage:
    """Stage the source this core-api ships with and register it as a candidate (the existing row when it is already known)."""
    return await data_plane.start_package_smoke(await data_plane.publish_package(name, actor))


async def _register(name: str, s3_key: str, actor: str) -> JobPackage:
    if name.startswith(TASK_PACKAGE_PREFIX):
        return await task_bundles.register_bundle(name, s3_key, actor)
    return await data_plane.start_package_smoke(await data_plane.register_staged_package(JobPackageName(name), s3_key, actor))


async def _promote(package: JobPackage, actor: str) -> JobPackage:
    if package.kind == PackageKind.TASK:
        return await task_bundles.promote_bundle(package, actor)
    async with (await get_pool()).acquire() as conn:
        return await job_packages.promote(conn, package.name, package.sha256, actor)


async def _bundle_targets_moved_since(core: JobPackage) -> bool:
    """The core's smoke ran against the bundle targets that were stable when it started (its `updated_at`): a bundle target
    promoted after that is one the core was not smoked with."""
    async with (await get_pool()).acquire() as conn:
        bundles = await job_packages.list_kind(conn, PackageKind.TASK, PackageStatus.STABLE)
    return any(b.updated_at > core.updated_at for b in bundles)


async def _move(package: JobPackage, actor: str) -> JobPackage:
    """Make a candidate whose smoke passed the target of its package. The core moves only while the bundle targets it was
    smoked with are still the stable ones; otherwise it is smoked again and that new candidate is returned."""
    if package.name == data_plane.CORE_PACKAGE and await _bundle_targets_moved_since(package):
        return await data_plane.restart_package_smoke(package)
    promoted = await _promote(package, actor)
    if promoted is None:
        raise not_found()
    return promoted


async def target(name: str, s3_key: str, actor: str) -> JobPackage:
    """CI's call for a version: register the staged zip, smoke it, and make it the target once its smoke passed. A version
    already known is not registered again: one that passed moves now (a rollback, or the same push again), one that failed is
    smoked again, and one still running is left to `promote_targets`."""
    package = await _register(name, s3_key, actor)
    if package.status == PackageStatus.STABLE:
        return package
    settled = await data_plane.refresh_package_smoke(package)
    if settled.smoke_status == SmokeStatus.PASSED:
        return await _move(settled, actor)
    if settled.smoke_status == SmokeStatus.FAILED and settled.status == PackageStatus.CANDIDATE:
        return await data_plane.retry_failed_package_smoke(settled, SMOKE_NOT_RETRYABLE_DETAIL)
    return settled


async def promote_targets() -> None:
    """Poller: the newest candidate whose smoke passed becomes its package's target. A candidate pushed before the current target
    does not replace it (an older push that settles late, or a rollback's target). A task bundle waits while its slug opted out
    or the bundle switch is off."""
    async with (await get_pool()).acquire() as conn:
        candidates = await job_packages.list_status(conn, PackageStatus.CANDIDATE)  # oldest first
        current = {p.name: p for p in await job_packages.list_status(conn, PackageStatus.STABLE)}
    for candidate in candidates:
        settled = await data_plane.refresh_package_smoke(candidate)
        if settled.smoke_status != SmokeStatus.PASSED:
            continue
        target_now = current.get(candidate.name)
        if target_now is not None and candidate.updated_at <= target_now.updated_at:
            continue
        if candidate.kind == PackageKind.TASK and not await task_bundles.auto_promote_allowed(TaskType(candidate.manifest["slug"])):
            continue
        moved = await _move(settled, AUTO_PROMOTER)
        if moved.status == PackageStatus.STABLE:
            current[candidate.name] = moved
            logger.info("job package %s %s became its target after its smoke passed", moved.name, moved.sha256)


async def set_status(name: JobPackageName, sha256: str, status: PackageStatus, actor: str) -> JobPackage:
    """A manual move: promote a candidate to stable (its smoke must have passed; the one it replaces is retired), or retire a
    package that is not the target. A manual promotion sticks: the poller only moves the target to a candidate pushed after it."""
    async with (await get_pool()).acquire() as conn:
        current = await job_packages.get(conn, name, sha256)
    if current is None:
        raise not_found()
    if status == PackageStatus.STABLE:
        settled = await data_plane.refresh_package_smoke(current)
        if settled.smoke_status != SmokeStatus.PASSED:
            raise Problem(HTTPStatus.CONFLICT, data_plane.SmokeProblem.NOT_PASSED, SMOKE_NOT_PASSED_DETAIL)
        return await _move(settled, actor)  # a core whose bundle targets moved is smoked again first, and that is returned
    if current.status == PackageStatus.STABLE:
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.STABLE_NOT_RETIRABLE, STABLE_NOT_RETIRABLE_DETAIL)
    async with (await get_pool()).acquire() as conn:
        package = await job_packages.retire(conn, name, sha256, actor, status)
    if package is None:
        raise not_found()
    return package


async def retry_smoke(name: JobPackageName, sha256: str) -> JobPackage:
    """Run the smoke job again for a candidate whose smoke failed; anything else is a 409."""
    async with (await get_pool()).acquire() as conn:
        current = await job_packages.get(conn, name, sha256)
    if current is None:
        raise not_found()
    return await data_plane.retry_failed_package_smoke(current, SMOKE_NOT_RETRYABLE_DETAIL)
