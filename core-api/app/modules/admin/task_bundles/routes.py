"""Task bundles, platform-wide: publishing one is a deploy (planning/25 "Security model"), so every route goes through
get_platform_admin, like /v1/admin/job-packages."""
import logging

from fastapi import APIRouter, Depends, Path

from app.core.auth import get_platform_admin
from app.core.enums import TaskType
from app.core.job_packages import SHA256_PATTERN, JobPackage
from app.modules.admin.task_bundles import service
from app.modules.admin.task_bundles.schemas import BundleSettings, BundleSettingsBody, BundleStatusBody, RegisterBody

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/task-bundles", tags=["admin"])


@router.post("/register", response_model=JobPackage)
async def register_bundle(body: RegisterBody, actor: str = Depends(get_platform_admin)):
    """Register a bundle CI put in the staging bucket: core-api reads it back, validates the zip and its manifest, records
    a candidate and starts its smoke on the stable core."""
    package = await service.register(body.s3_key, actor)
    logger.info("task bundle %s %s registered by %s", package.name, package.sha256, actor)
    return package


@router.get("/{slug}/settings", response_model=BundleSettings)
async def get_bundle_settings(slug: TaskType, actor: str = Depends(get_platform_admin)):
    return await service.get_settings(slug)


@router.put("/{slug}/settings", response_model=BundleSettings)
async def put_bundle_settings(slug: TaskType, body: BundleSettingsBody, actor: str = Depends(get_platform_admin)):
    """auto_promote=false keeps a person in the loop for this task: a bundle whose smoke passed waits for a PUT .../status."""
    result = await service.put_settings(slug, body.auto_promote, actor)
    logger.info("task bundle settings of %s set by %s: auto_promote=%s", slug, actor, body.auto_promote)
    return result


@router.put("/{slug}/{sha256}/status", response_model=JobPackage)
async def set_bundle_status(slug: TaskType, body: BundleStatusBody, sha256: str = Path(pattern=SHA256_PATTERN), actor: str = Depends(get_platform_admin)):
    """Promote a bundle whose smoke passed (409 otherwise): it becomes the task's only stable bundle and the task's row and
    presets are rewritten from its manifest. Promoting an older sha is the rollback. Retiring the stable one is a 409."""
    package = await service.set_status(slug, sha256, body.status, actor)
    logger.info("task bundle %s %s set to %s by %s", package.name, sha256, body.status, actor)
    return package
