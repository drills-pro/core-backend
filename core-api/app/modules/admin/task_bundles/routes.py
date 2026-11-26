"""Task bundles, platform-wide. A bundle is registered and moved to its task's target through
/v1/admin/job-packages/task_<slug>/target; these routes read the tasks and their switches. Every route goes through
get_platform_admin. Fixed paths (/catalog, .../settings) come before the parameterised ones that would otherwise match them."""
import logging

from fastapi import APIRouter, Depends, Path

from app.core.auth import get_platform_admin
from app.core.enums import TaskType
from app.core.patterns import SHA256_PATTERN
from app.core.presets import NAME_PATTERN, TaskPreset
from app.core.task_types import TaskInfo
from app.modules.admin.task_bundles import service
from app.modules.admin.task_bundles.schemas import BundleSettings, BundleSettingsBody, BundleStatusBody, CatalogTask, EnabledBody, TaskBundle

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/task-bundles", tags=["admin"])


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
    """auto_promote=false keeps a person in the loop for this task: a bundle whose smoke passed waits for a target call."""
    result = await service.put_settings(slug, body.auto_promote, actor)
    logger.info("task bundle settings of %s set by %s: auto_promote=%s", slug, actor, body.auto_promote)
    return result


@router.get("/{slug}/{sha256}", response_model=TaskBundle)
async def get_bundle(slug: TaskType, sha256: str = Path(pattern=SHA256_PATTERN), actor: str = Depends(get_platform_admin)):
    """One bundle; CI polls this for smoke_status after its target call."""
    return await service.get_bundle(slug, sha256)


@router.put("/{slug}/{sha256}/status", response_model=TaskBundle)
async def set_bundle_status(slug: TaskType, body: BundleStatusBody, sha256: str = Path(pattern=SHA256_PATTERN), actor: str = Depends(get_platform_admin)):
    """A manual move: promote a bundle whose smoke passed (409 otherwise) or retire one that is not stable. Promoting it rewrites the
    task's row and presets from its manifest; promoting an older sha is the rollback. A manual promotion sticks until CI's next bundle."""
    bundle = await service.set_status(slug, sha256, body.status, actor)
    logger.info("task bundle %s %s set to %s by %s", bundle.name, sha256, body.status, actor)
    return bundle


@router.post("/{slug}/{sha256}/smoke", response_model=TaskBundle)
async def retry_bundle_smoke(slug: TaskType, sha256: str = Path(pattern=SHA256_PATTERN), actor: str = Depends(get_platform_admin)):
    """Smoke a candidate whose smoke failed again (409 for anything else)."""
    bundle = await service.retry_smoke(slug, sha256)
    logger.info("task bundle %s %s smoke retried by %s as %s", bundle.name, sha256, actor, bundle.smoke_job_id)
    return bundle
