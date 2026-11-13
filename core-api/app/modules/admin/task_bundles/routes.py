"""Task bundles, platform-wide: publishing one is a deploy (planning/25 "Security model"), so every route goes through
get_platform_admin, like /v1/admin/job-packages."""
import logging

from fastapi import APIRouter, Depends

from app.core.auth import get_platform_admin
from app.core.job_packages import JobPackage
from app.modules.admin.task_bundles import service
from app.modules.admin.task_bundles.schemas import RegisterBody

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/task-bundles", tags=["admin"])


@router.post("/register", response_model=JobPackage)
async def register_bundle(body: RegisterBody, actor: str = Depends(get_platform_admin)):
    """Register a bundle CI put in the staging bucket: core-api reads it back, validates the zip and its manifest, records
    a candidate and starts its smoke on the stable core."""
    package = await service.register(body.s3_key, actor)
    logger.info("task bundle %s %s registered by %s", package.name, package.sha256, actor)
    return package
