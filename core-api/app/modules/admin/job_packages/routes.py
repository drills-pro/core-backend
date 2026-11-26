"""Platform-wide job packages. Rows are shared by every tenant, so these go through get_platform_admin and an
unscoped connection; the packages run by training and ingest are chosen in app/integrations/data_plane/job_package.py.
CI's target call makes a version its package's target; an operator can also promote a passed candidate by hand (PUT .../status),
and that manual promotion sticks until CI registers a newer version."""
import logging

from fastapi import APIRouter, Depends, Path

from app.core.auth import get_platform_admin
from app.core.enums import JobPackageName
from app.core.job_packages import JobPackage
from app.core.patterns import SHA256_PATTERN
from app.modules.admin.job_packages import service
from app.modules.admin.job_packages.constants import TARGET_NAME_PATTERN
from app.modules.admin.job_packages.schemas import StatusBody, TargetBody

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/job-packages", tags=["admin"])


@router.get("/{name}", response_model=list[JobPackage])
async def list_packages(name: JobPackageName, actor: str = Depends(get_platform_admin)):
    return await service.list_packages(name)


@router.post("/{name}/publish", response_model=JobPackage)
async def publish_package(name: JobPackageName, actor: str = Depends(get_platform_admin)):
    """Stage the source this core-api ships with and register it as a candidate (the existing row when it is already known)."""
    package = await service.publish(name, actor)
    logger.info("job package %s %s published by %s", name, package.sha256, actor)
    return package


@router.post("/{name}/target", response_model=JobPackage)
async def target_package(body: TargetBody, name: str = Path(pattern=TARGET_NAME_PATTERN), actor: str = Depends(get_platform_admin)):
    """CI's call for a version: the staged zip is checked, registered, smoked, and becomes the target of its package once the smoke
    passed (the poller moves it). A version that already passed moves at once, which is how a rollback is made. Nobody has to act
    in between: a failed smoke is run again by the next call."""
    package = await service.target(name, body.s3_key, actor)
    logger.info("job package %s %s targeted by %s: status %s, smoke %s", name, package.sha256, actor, package.status, package.smoke_status)
    return package


@router.put("/{name}/{sha256}/status", response_model=JobPackage)
async def set_package_status(name: JobPackageName, body: StatusBody, sha256: str = Path(pattern=SHA256_PATTERN), actor: str = Depends(get_platform_admin)):
    """A manual move: promote a candidate whose smoke passed (the one it replaces is retired) or retire a package that is not the
    target. A manual promotion sticks until CI registers a newer version."""
    package = await service.set_status(name, sha256, body.status, actor)
    logger.info("job package %s %s set to %s by %s", name, sha256, body.status, actor)
    return package


@router.post("/{name}/{sha256}/smoke", response_model=JobPackage)
async def retry_package_smoke(name: JobPackageName, sha256: str = Path(pattern=SHA256_PATTERN), actor: str = Depends(get_platform_admin)):
    """Run the smoke job again for a candidate whose smoke failed (after the Ray image was fixed). Anything else is a 409."""
    package = await service.retry_smoke(name, sha256)
    logger.info("job package %s %s smoke retried by %s as %s", name, sha256, actor, package.smoke_job_id)
    return package
