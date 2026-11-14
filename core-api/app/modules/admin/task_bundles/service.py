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
from app.core.presets import TaskPreset
from app.core.task_types import TaskInfo
from app.modules.admin.task_bundles.schemas import BundleSettings, CatalogTask, TaskBundle

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
        serving = manifest.serving and (manifest.serving.encoder, manifest.serving.postprocess)
        await task_types.apply_bundle(conn, manifest.slug, manifest.data_kind, manifest.trainer, manifest.servable, serving, bundle.sha256, actor)
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


async def list_bundles(slug: TaskType | None = None) -> list[TaskBundle]:
    """Every bundle (of `slug` when given), smoke settled, newest change last per slug."""
    async with (await get_pool()).acquire() as conn:
        bundles = await job_packages.list_kind(conn, PackageKind.TASK)
    name = slug and task_package_name(slug)
    return [TaskBundle.of(await data_plane.refresh_package_smoke(b)) for b in bundles if name is None or b.name == name]


async def get_bundle(slug: TaskType, sha256: str) -> TaskBundle:
    async with (await get_pool()).acquire() as conn:
        bundle = await _bundle(conn, slug, sha256)
    return TaskBundle.of(await data_plane.refresh_package_smoke(bundle))


async def retry_smoke(slug: TaskType, sha256: str) -> TaskBundle:
    """Smoke a candidate whose smoke failed again (after the core or the image was fixed); anything else is a 409."""
    async with (await get_pool()).acquire() as conn:
        bundle = await _bundle(conn, slug, sha256)
    not_retryable = Problem(HTTPStatus.CONFLICT, ProblemCode.SMOKE_NOT_RETRYABLE, "only a candidate whose smoke failed can be smoked again")
    if bundle.status != PackageStatus.CANDIDATE or (await data_plane.refresh_package_smoke(bundle)).smoke_status != SmokeStatus.FAILED:
        raise not_retryable
    retried = await data_plane.retry_package_smoke(bundle.name, sha256)
    if retried is None:  # another retry or a promotion got there first
        raise not_retryable
    return TaskBundle.of(retried)


async def catalog() -> list[CatalogTask]:
    """The task_types rows with their presets, the stable bundle behind each and its auto-promote switch."""
    async with (await get_pool()).acquire() as conn:
        tasks = await task_types.list_all(conn)
        stable = {b.sha256: b for b in await job_packages.list_kind(conn, PackageKind.TASK, PackageStatus.STABLE)}
        return [
            CatalogTask(task=t, presets=await presets.list_for(conn, t.slug), auto_promote=(await _settings(conn, t.slug)).auto_promote,
                        bundle=TaskBundle.of(stable[t.bundle_sha256]) if t.bundle_sha256 in stable else None)
            for t in tasks]


async def set_task_enabled(slug: TaskType, enabled: bool, actor: str) -> TaskInfo:
    async with (await get_pool()).acquire() as conn:
        info = await task_types.set_enabled(conn, slug, enabled, actor)
    if info is None:
        raise not_found()
    return info


async def set_preset_enabled(slug: TaskType, name: str, enabled: bool, actor: str) -> TaskPreset:
    async with (await get_pool()).acquire() as conn:
        preset = await presets.set_enabled(conn, slug, name, enabled, actor)
    if preset is None:
        raise not_found()
    return preset


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
