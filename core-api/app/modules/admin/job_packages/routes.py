"""Platform-wide job packages. Rows are shared by every tenant, so these go through get_platform_admin and an
unscoped connection; the packages run by training and ingest are chosen in app/integrations/data_plane/job_package.py."""
import logging
from http import HTTPStatus

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict

from app.core import job_packages
from app.core.auth import get_platform_admin
from app.core.db import get_pool
from app.core.enums import JobPackageName, PackageStatus, SmokeStatus
from app.core.job_packages import SHA256_PATTERN, JobPackage
from app.core.problem import Problem, not_found
from app.integrations import data_plane

SMOKE_NOT_PASSED = "smoke_not_passed"
SMOKE_NOT_RETRYABLE = "smoke_not_retryable"

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/job-packages", tags=["admin"])


class StatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: PackageStatus


class RegisterBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    s3_key: str  # in the staging bucket: jobs/<ray-jobs|ray-training>-<sha256>.zip


@router.get("/{name}", response_model=list[JobPackage])
async def list_packages(name: JobPackageName, actor: str = Depends(get_platform_admin)):
    async with (await get_pool()).acquire() as conn:
        packages = await job_packages.list_for(conn, name)
    return [await data_plane.refresh_package_smoke(p) for p in packages]


@router.post("/{name}/publish", response_model=JobPackage)
async def publish_package(name: JobPackageName, actor: str = Depends(get_platform_admin)):
    """Stage the source this core-api ships with and register it as a candidate (the existing row when it is already known)."""
    package = await data_plane.start_package_smoke(await data_plane.publish_package(name, actor))
    logger.info("job package %s %s published by %s", name, package.sha256, actor)
    return package


@router.post("/{name}/register", response_model=JobPackage)
async def register_package(name: JobPackageName, body: RegisterBody, actor: str = Depends(get_platform_admin)):
    """Register a zip CI already put in the staging bucket as a candidate: the body names only its key, and core-api reads
    the object back and checks its hash and contents."""
    package = await data_plane.start_package_smoke(await data_plane.register_staged_package(name, body.s3_key, actor))
    logger.info("job package %s %s registered by %s", name, package.sha256, actor)
    return package


@router.put("/{name}/{sha256}/status", response_model=JobPackage)
async def set_package_status(name: JobPackageName, body: StatusBody, sha256: str = Path(pattern=SHA256_PATTERN), actor: str = Depends(get_platform_admin)):
    """Promote a candidate to stable (new runs use the newest stable) or retire a package. Runs already created keep theirs.
    A ray_training (core) smoke ran every bundle stable when it started, so passing it is the core x bundle gate of planning/25."""
    # ponytail: a bundle promoted while the core's smoke ran is not in it; re-smoke the core if that matters
    async with (await get_pool()).acquire() as conn:
        current = await job_packages.get(conn, name, sha256) if body.status == PackageStatus.STABLE else None
    if current is not None and (await data_plane.refresh_package_smoke(current)).smoke_status != SmokeStatus.PASSED:
        raise Problem(HTTPStatus.CONFLICT, SMOKE_NOT_PASSED, "the package's smoke job has not passed")
    async with (await get_pool()).acquire() as conn:
        if body.status == PackageStatus.STABLE:
            package = await job_packages.promote(conn, name, sha256, actor)  # retires the previous stable
        else:
            package = await job_packages.set_status(conn, name, sha256, body.status, actor)
    if package is None:
        raise not_found()
    logger.info("job package %s %s set to %s by %s", name, sha256, body.status, actor)
    return package


@router.post("/{name}/{sha256}/smoke", response_model=JobPackage)
async def retry_package_smoke(name: JobPackageName, sha256: str = Path(pattern=SHA256_PATTERN), actor: str = Depends(get_platform_admin)):
    """Run the smoke job again for a candidate whose smoke failed (after the Ray image was fixed). Anything else is a 409."""
    async with (await get_pool()).acquire() as conn:
        current = await job_packages.get(conn, name, sha256)
    if current is None:
        raise not_found()
    not_retryable = Problem(HTTPStatus.CONFLICT, SMOKE_NOT_RETRYABLE, "only a candidate package whose smoke job failed can be smoked again")
    if current.status != PackageStatus.CANDIDATE or (await data_plane.refresh_package_smoke(current)).smoke_status != SmokeStatus.FAILED:
        raise not_retryable
    package = await data_plane.retry_package_smoke(name, sha256)
    if package is None:  # another retry or a promotion got there first
        raise not_retryable
    logger.info("job package %s %s smoke retried by %s as %s", name, sha256, actor, package.smoke_job_id)
    return package
