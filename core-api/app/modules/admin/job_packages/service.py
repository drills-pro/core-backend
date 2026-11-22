"""The admin's moves on a runtime package: publish or register a candidate, promote, retire, smoke again. The rules (smoke
gate, one stable, audit) are the lifecycle's (data_plane/job_package.py, core/job_packages.py); each write here is one
transaction. A gateway call (Ray, S3) never runs inside a pooled connection, so a promotion reads the row, settles its
smoke, then writes in a connection of its own."""
from app.core import job_packages
from app.core.db import get_pool
from app.core.enums import JobPackageName, PackageStatus
from app.core.job_packages import JobPackage
from app.core.problem import not_found
from app.integrations import data_plane
from app.modules.admin.job_packages.constants import SMOKE_NOT_PASSED_DETAIL, SMOKE_NOT_RETRYABLE_DETAIL


async def list_packages(name: JobPackageName) -> list[JobPackage]:
    async with (await get_pool()).acquire() as conn:
        packages = await job_packages.list_for(conn, name)
    return [await data_plane.refresh_package_smoke(p) for p in packages]


async def publish(name: JobPackageName, actor: str) -> JobPackage:
    """Stage the source this core-api ships with and register it as a candidate (the existing row when it is already known)."""
    return await data_plane.start_package_smoke(await data_plane.publish_package(name, actor))


async def register(name: JobPackageName, s3_key: str, actor: str) -> JobPackage:
    return await data_plane.start_package_smoke(await data_plane.register_staged_package(name, s3_key, actor))


async def set_status(name: JobPackageName, sha256: str, status: PackageStatus, actor: str) -> JobPackage:
    """Promote a candidate to stable (once its smoke passed; the previous stable is retired) or retire a package."""
    # ponytail: a bundle promoted while the core's smoke ran is not in it; re-smoke the core if that matters
    if status == PackageStatus.STABLE:
        async with (await get_pool()).acquire() as conn:
            current = await job_packages.get(conn, name, sha256)
        if current is not None:
            await data_plane.require_package_smoke_passed(current, SMOKE_NOT_PASSED_DETAIL)
        async with (await get_pool()).acquire() as conn:
            package = await job_packages.promote(conn, name, sha256, actor)
    else:
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
