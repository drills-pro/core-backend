"""Task bundles, platform-wide: publishing one is a deploy (planning/25 "Security model"), so every route goes through
get_platform_admin, like /v1/admin/job-packages. Fixed paths (/register, /catalog, .../settings) come before the
parameterised ones that would otherwise match them."""
import logging

from fastapi import APIRouter, Depends, Path

from app.core.auth import get_platform_admin
from app.core.enums import TaskType
from app.core.job_packages import SHA256_PATTERN
from app.core.presets import NAME_PATTERN, TaskPreset
from app.core.task_types import TaskInfo
from app.modules.admin.task_bundles import service
from app.modules.admin.task_bundles.schemas import (
    BundleSettings,
    BundleSettingsBody,
    BundleStatusBody,
    CatalogTask,
    EnabledBody,
    RegisterBody,
    TaskBundle,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/task-bundles", tags=["admin"])


@router.post("/register", response_model=TaskBundle)
async def register_bundle(body: RegisterBody, actor: str = Depends(get_platform_admin)):
    """Register a bundle CI put in the staging bucket: core-api reads it back, validates the zip and its manifest, records
    a candidate and starts its smoke on the stable core."""
    package = await service.register(body.s3_key, actor)
    logger.info("task bundle %s %s registered by %s", package.name, package.sha256, actor)
    return TaskBundle.of(package)


@router.get("", response_model=list[TaskBundle])
async def list_bundles(actor: str = Depends(get_platform_admin)):
    return await service.list_bundles()


@router.get("/catalog", response_model=list[CatalogTask])
async def task_catalog(actor: str = Depends(get_platform_admin)):
    """What tenants can train: each task_types row with its presets and the stable bundle that wrote them."""
    return await service.catalog()


@router.put("/catalog/{slug}/enabled", response_model=TaskInfo)
async def set_task_enabled(slug: TaskType, body: EnabledBody, actor: str = Depends(get_platform_admin)):
    """Switch a task off for new runs and back on; a promotion leaves the switch as it is."""
    info = await service.set_task_enabled(slug, body.enabled, actor)
    logger.info("task %s enabled=%s by %s", slug, body.enabled, actor)
    return info


@router.put("/catalog/{slug}/presets/{name}/enabled", response_model=TaskPreset)
async def set_preset_enabled(slug: TaskType, body: EnabledBody, name: str = Path(pattern=NAME_PATTERN), actor: str = Depends(get_platform_admin)):
    """Switch one preset off (hidden from tenants, refused for new runs) and back on; a promotion leaves the switch as it is."""
    preset = await service.set_preset_enabled(slug, name, body.enabled, actor)
    logger.info("preset %s/%s enabled=%s by %s", slug, name, body.enabled, actor)
    return preset


@router.get("/{slug}", response_model=list[TaskBundle])
async def list_slug_bundles(slug: TaskType, actor: str = Depends(get_platform_admin)):
    return await service.list_bundles(slug)


@router.get("/{slug}/settings", response_model=BundleSettings)
async def get_bundle_settings(slug: TaskType, actor: str = Depends(get_platform_admin)):
    return await service.get_settings(slug)


@router.put("/{slug}/settings", response_model=BundleSettings)
async def put_bundle_settings(slug: TaskType, body: BundleSettingsBody, actor: str = Depends(get_platform_admin)):
    """auto_promote=false keeps a person in the loop for this task: a bundle whose smoke passed waits for a PUT .../status."""
    result = await service.put_settings(slug, body.auto_promote, actor)
    logger.info("task bundle settings of %s set by %s: auto_promote=%s", slug, actor, body.auto_promote)
    return result


@router.get("/{slug}/{sha256}", response_model=TaskBundle)
async def get_bundle(slug: TaskType, sha256: str = Path(pattern=SHA256_PATTERN), actor: str = Depends(get_platform_admin)):
    """One bundle; CI polls this for smoke_status after register."""
    return await service.get_bundle(slug, sha256)


@router.put("/{slug}/{sha256}/status", response_model=TaskBundle)
async def set_bundle_status(slug: TaskType, body: BundleStatusBody, sha256: str = Path(pattern=SHA256_PATTERN), actor: str = Depends(get_platform_admin)):
    """Promote a bundle whose smoke passed (409 otherwise): it becomes the task's only stable bundle and the task's row and
    presets are rewritten from its manifest. Promoting an older sha is the rollback. Retiring the stable one is a 409."""
    package = await service.set_status(slug, sha256, body.status, actor)
    logger.info("task bundle %s %s set to %s by %s", package.name, sha256, body.status, actor)
    return TaskBundle.of(package)


@router.post("/{slug}/{sha256}/smoke", response_model=TaskBundle)
async def retry_bundle_smoke(slug: TaskType, sha256: str = Path(pattern=SHA256_PATTERN), actor: str = Depends(get_platform_admin)):
    """Smoke a candidate whose smoke failed again (409 for anything else)."""
    bundle = await service.retry_smoke(slug, sha256)
    logger.info("task bundle %s %s smoke retried by %s as %s", bundle.name, sha256, actor, bundle.smoke_job_id)
    return bundle
