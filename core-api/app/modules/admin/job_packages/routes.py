"""Platform-wide job packages. Rows are shared by every tenant, so these go through get_platform_admin and an
unscoped connection; the packages run by training and ingest are chosen in app/integrations/data_plane/job_package.py."""
import logging

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict

from app.core import job_packages
from app.core.auth import get_platform_admin
from app.core.db import get_pool
from app.core.enums import JobPackageName, PackageStatus
from app.core.job_packages import SHA256_PATTERN, JobPackage
from app.core.problem import not_found
from app.integrations import data_plane

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/job-packages", tags=["admin"])


class StatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: PackageStatus


@router.get("/{name}", response_model=list[JobPackage])
async def list_packages(name: JobPackageName, actor: str = Depends(get_platform_admin)):
    async with (await get_pool()).acquire() as conn:
        return await job_packages.list_for(conn, name)


@router.post("/{name}/publish", response_model=JobPackage)
async def publish_package(name: JobPackageName, actor: str = Depends(get_platform_admin)):
    """Stage the source this core-api ships with and register it as a candidate (the existing row when it is already known)."""
    package = await data_plane.publish_package(name, actor)
    logger.info("job package %s %s published by %s", name, package.sha256, actor)
    return package


@router.put("/{name}/{sha256}/status", response_model=JobPackage)
async def set_package_status(name: JobPackageName, body: StatusBody, sha256: str = Path(pattern=SHA256_PATTERN), actor: str = Depends(get_platform_admin)):
    """Promote a candidate to stable (new runs use the newest stable) or retire a package. Runs already created keep theirs."""
    async with (await get_pool()).acquire() as conn:
        package = await job_packages.set_status(conn, name, sha256, body.status, actor)
    if package is None:
        raise not_found()
    logger.info("job package %s %s set to %s by %s", name, sha256, body.status, actor)
    return package
