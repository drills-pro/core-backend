"""Task bundles (planning/25): what a task's bundle writes when it becomes the target (the task_types row and the task's
presets, in the same transaction as the package move), the per-slug opt-out, and the reads. The lifecycle itself (register,
smoke, target) is core/job_packages and admin/job_packages.
"""
from app.core import job_packages, presets, task_types
from app.core.bundle_manifest import BundleManifest
from app.core.db import get_pool
from http import HTTPStatus

from app.core.enums import PackageKind, PackageStatus, SmokeStatus, TaskType
from app.core.job_packages import JobPackage, task_package_name
from app.core.problem import Problem, not_found
from app.core.settings import settings
from app.integrations import data_plane
from app.modules.admin.task_bundles.constants import (
    BUNDLE_IS_STABLE_DETAIL,
    DEFAULT_AUTO_PROMOTE,
    SMOKE_NOT_PASSED_DETAIL,
    SMOKE_NOT_RETRYABLE_DETAIL,
    ProblemCode,
)
from app.core.presets import TaskPreset
from app.core.task_types import TaskInfo
from app.modules.admin.task_bundles.schemas import BundleSettings, CatalogTask, TaskBundle

_SETTINGS = "SELECT * FROM task_bundle_settings WHERE slug = $1"
_PUT_SETTINGS = (
    "INSERT INTO task_bundle_settings (slug, auto_promote, updated_by) VALUES ($1, $2, $3) "
    "ON CONFLICT (slug) DO UPDATE SET auto_promote = EXCLUDED.auto_promote, updated_by = EXCLUDED.updated_by, updated_at = now() RETURNING *"
)


async def register(name: str, s3_key: str, actor: str) -> JobPackage:
    """Validate the staged zip, record it as a candidate of `name` (audited) and start its smoke; a known bundle comes back as it is."""
    return await data_plane.start_package_smoke(await data_plane.register_staged_bundle(name, s3_key, actor))


async def promote(bundle: JobPackage, actor: str) -> JobPackage:
    """Make a bundle the task's stable one: the package move, the task's row and its presets, in one transaction."""
    manifest = BundleManifest.model_validate(bundle.manifest)
    async with (await get_pool()).acquire() as conn, conn.transaction():
        promoted = await job_packages.promote(conn, bundle.name, bundle.sha256, actor)
        serving = manifest.serving and (manifest.serving.encoder, manifest.serving.postprocess)
        await task_types.apply_bundle(conn, manifest.slug, manifest.data_kind, manifest.trainer, manifest.servable, serving, bundle.sha256, actor)
        await presets.delete_others(conn, manifest.slug, [p.name for p in manifest.presets])
        for preset in manifest.presets:
            await presets.put(conn, manifest.slug, preset.name, preset, actor)
    return promoted


async def _bundle(conn, slug: TaskType, sha256: str) -> JobPackage:
    bundle = await job_packages.get(conn, task_package_name(slug), sha256)
    if bundle is None or bundle.kind != PackageKind.TASK:
        raise not_found()
    return bundle


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
    return TaskBundle.of(await data_plane.retry_failed_package_smoke(bundle, SMOKE_NOT_RETRYABLE_DETAIL))


async def set_status(slug: TaskType, sha256: str, status: PackageStatus, actor: str) -> TaskBundle:
    """A manual move: promote a bundle whose smoke passed (it becomes the task's stable one, and its row and presets are rewritten
    from its manifest), or retire one that is not stable. A manual promotion sticks until CI registers a newer bundle."""
    async with (await get_pool()).acquire() as conn:
        bundle = await _bundle(conn, slug, sha256)
    if status == PackageStatus.STABLE:
        settled = await data_plane.refresh_package_smoke(bundle)
        if settled.smoke_status != SmokeStatus.PASSED:
            raise Problem(HTTPStatus.CONFLICT, data_plane.SmokeProblem.NOT_PASSED, SMOKE_NOT_PASSED_DETAIL)
        return TaskBundle.of(await promote(settled, actor))
    if bundle.status == PackageStatus.STABLE:
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.BUNDLE_IS_STABLE, BUNDLE_IS_STABLE_DETAIL)
    async with (await get_pool()).acquire() as conn:
        package = await job_packages.retire(conn, bundle.name, sha256, actor, status)
    if package is None:
        raise not_found()
    return TaskBundle.of(package)


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


async def auto_promote_allowed(slug: TaskType) -> bool:
    """The poller may move this slug's target: the bundle switch is on and the slug did not opt out."""
    if not settings.training.bundle_auto_promote:
        return False
    async with (await get_pool()).acquire() as conn:
        return (await _settings(conn, slug)).auto_promote
