"""Task bundles (planning/25): register a staged bundle, smoke it on the stable core, promote it.

Promotion is one transaction: the slug's previous stable bundle is retired, this one becomes stable, and the task_types row
and the task's presets are rewritten from its manifest, with an audit row. Rollback is the same call on an older sha.
"""
import logging
from http import HTTPStatus

from app.core import job_packages, presets, task_types
from app.core.bundle_manifest import BundleManifest
from app.core.db import get_pool
from app.core.enums import PackageAuditAction, PackageKind, PackageStatus, SmokeStatus, TaskType
from app.core.job_packages import JobPackage, task_package_name
from app.core.problem import Problem, not_found
from app.core.settings import settings
from app.integrations import data_plane
from app.modules.admin.task_bundles.constants import AUTO_PROMOTER, DEFAULT_AUTO_PROMOTE, ProblemCode
from app.modules.admin.task_bundles.schemas import BundleSettings

logger = logging.getLogger(__name__)

_SETTINGS = "SELECT * FROM task_bundle_settings WHERE slug = $1"
_PUT_SETTINGS = (
    "INSERT INTO task_bundle_settings (slug, auto_promote, updated_by) VALUES ($1, $2, $3) "
    "ON CONFLICT (slug) DO UPDATE SET auto_promote = EXCLUDED.auto_promote, updated_by = EXCLUDED.updated_by, updated_at = now() RETURNING *"
)


async def register(s3_key: str, actor: str) -> JobPackage:
    """Validate the staged zip, record it as a candidate (audited) and start its smoke; a known bundle comes back as it is."""
    return await data_plane.start_package_smoke(await data_plane.register_staged_bundle(s3_key, actor))


async def _promote(conn, bundle: JobPackage, actor: str) -> JobPackage:
    manifest = BundleManifest.model_validate(bundle.manifest)
    async with conn.transaction():
        await job_packages.retire_stable_except(conn, bundle.name, bundle.sha256, actor)
        promoted = await job_packages.set_status(conn, bundle.name, bundle.sha256, PackageStatus.STABLE, actor)
        await task_types.apply_bundle(conn, manifest.slug, manifest.data_kind, manifest.trainer, manifest.servable, bundle.sha256, actor)
        await presets.delete_others(conn, manifest.slug, [p.name for p in manifest.presets])
        for preset in manifest.presets:
            await presets.put(conn, manifest.slug, preset.name, preset, actor)
        await job_packages.record(conn, bundle.name, bundle.sha256, PackageAuditAction.PROMOTE, actor)
    return promoted


async def _bundle(conn, slug: TaskType, sha256: str) -> JobPackage:
    bundle = await job_packages.get(conn, task_package_name(slug), sha256)
    if bundle is None or bundle.kind != PackageKind.TASK:
        raise not_found()
    return bundle


async def set_status(slug: TaskType, sha256: str, status: PackageStatus, actor: str) -> JobPackage:
    """Promote (only once the smoke passed) or retire (never the stable one: promote another sha instead)."""
    async with (await get_pool()).acquire() as conn:
        bundle = await _bundle(conn, slug, sha256)
    if status == PackageStatus.STABLE:
        if (await data_plane.refresh_package_smoke(bundle)).smoke_status != SmokeStatus.PASSED:
            raise Problem(HTTPStatus.CONFLICT, ProblemCode.SMOKE_NOT_PASSED, "the bundle's smoke job has not passed")
        async with (await get_pool()).acquire() as conn:
            return await _promote(conn, bundle, actor)
    if bundle.status == PackageStatus.STABLE:
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.BUNDLE_IS_STABLE, "the stable bundle of a task is replaced by promoting another sha")
    async with (await get_pool()).acquire() as conn, conn.transaction():
        retired = await job_packages.set_status(conn, bundle.name, sha256, status, actor)
        await job_packages.record(conn, bundle.name, sha256, PackageAuditAction.RETIRE, actor)
    return retired


async def _settings(conn, slug: TaskType) -> BundleSettings:
    row = await conn.fetchrow(_SETTINGS, slug)
    return BundleSettings(slug=slug, auto_promote=DEFAULT_AUTO_PROMOTE) if row is None else BundleSettings.model_validate(dict(row))


async def get_settings(slug: TaskType) -> BundleSettings:
    async with (await get_pool()).acquire() as conn:
        return await _settings(conn, slug)


async def put_settings(slug: TaskType, auto_promote: bool, actor: str) -> BundleSettings:
    async with (await get_pool()).acquire() as conn:
        return BundleSettings.model_validate(dict(await conn.fetchrow(_PUT_SETTINGS, slug, auto_promote, actor)))


async def auto_promote() -> None:
    """Poller: settle the smoke of each candidate bundle and promote the ones that passed, unless their slug opted out.
    Oldest first, so of two passing candidates of a slug the newer ends up stable."""
    if not settings.training.bundle_auto_promote:
        return
    async with (await get_pool()).acquire() as conn:
        candidates = await job_packages.list_kind(conn, PackageKind.TASK, PackageStatus.CANDIDATE)
    for candidate in candidates:
        if (await data_plane.refresh_package_smoke(candidate)).smoke_status != SmokeStatus.PASSED:
            continue
        async with (await get_pool()).acquire() as conn:
            if not (await _settings(conn, TaskType(candidate.manifest["slug"]))).auto_promote:
                continue
            await _promote(conn, candidate, AUTO_PROMOTER)
        logger.info("task bundle %s %s promoted after its smoke passed", candidate.name, candidate.sha256)
