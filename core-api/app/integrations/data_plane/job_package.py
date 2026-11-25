"""The lifecycle of the job packages (`ray-jobs/ray_jobs`, `ray-training/ray_core` as ray_training, `ray-serving/ray_serving`):
a zip in the staging bucket named by its content hash, registered in `job_packages` as a candidate, smoked on Ray, and made
the package's target (stable) once that smoke passed; the one it replaces is retired. The zips are built and checked in package_zip.py, the bucket is
package_store.py, the rows and their audit trail core/job_packages.py; this module is the rules that join them.

`register_staged` registers the zip CI put in staging and `publish` builds one from the source tree the image ships with.
Jobs run a registered package: `presigned_url` hands the Ray job a GET for it and the job holds no extra credential.
Outside prod, `resolve_stable` publishes and promotes the bundled source itself (audited as SELF_PUBLISHER), so a checkout
runs without an admin step.
"""
import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from http import HTTPStatus

import httpx

from app.core import job_packages, secret_files
from app.core.db import get_pool
from app.core.enums import JobPackageName, PackageKind, PackageStatus, SmokeStatus
from app.core.job_packages import JobPackage
from app.core.problem import Problem
from app.core.requirements import requirements_sha256
from app.core.settings import settings
from app.integrations import compute
from app.integrations.data_plane import job_catalog, job_submit, lakefs, layout, package_store
from app.integrations.data_plane.package_zip import PACKAGE_DIRS, build, invalid, package_key, validate_package

SELF_PUBLISHER = "self-publish"  # the actor recorded when a non-prod core-api registers and promotes its own source
PROD_ENV = "prod"
NO_STABLE_PACKAGE = "no_stable_package"


class SmokeProblem(StrEnum):
    NOT_RETRYABLE = "smoke_not_retryable"  # only a candidate whose smoke failed is smoked again


SMOKE_ATTEMPT_FORMAT = "%Y%m%d%H%M%S"  # the suffix of a retried smoke job id: when the retry started
SMOKE_TIMEOUT_SECONDS = 1800  # a tiny run, but the first one on a node installs its pinned environment (about 7 minutes cold for the core): the ceiling for a stuck one
_SMOKE_DEAD = (compute.JobStatus.FAILED, compute.JobStatus.STOPPED)
CORE = JobPackageName.RAY_TRAINING  # the package that holds ray_core, the contract every task bundle runs on


async def publish(name: JobPackageName, actor: str) -> JobPackage:
    """Stage the bundled source of `name` if this version is not in staging yet and register it (a candidate when new)."""
    data, key = build(PACKAGE_DIRS[name])
    await asyncio.to_thread(package_store.upload_once, data, key)
    async with (await get_pool()).acquire() as conn:
        return await job_packages.register(conn, name, hashlib.sha256(data).hexdigest(), key, actor)


async def register_staged(name: JobPackageName, s3_key: str, actor: str) -> JobPackage:
    """Register a zip CI already put in staging (`jobs/<dir>-<sha256>.zip`) as a candidate. The key must be the one the
    package's name and hash give, and the object is read back and checked, so a key proves nothing by itself."""
    package_dir = PACKAGE_DIRS[name]
    match = re.fullmatch(rf"{re.escape(package_key(package_dir, '@'))}".replace("@", "([0-9a-f]{64})"), s3_key)
    if match is None:
        raise invalid(f"expected a key like {package_key(package_dir, '<sha256>')}")
    sha256 = match.group(1)
    data = await asyncio.to_thread(package_store.read_staged, s3_key)
    requirements = validate_package(name, data, sha256)
    async with (await get_pool()).acquire() as conn:
        return await job_packages.register(conn, name, sha256, s3_key, actor, requirements)


def _smoke_base_id(package: JobPackage) -> str:
    return f"smoke-{package.name.replace('_', '-')}-{package.sha256[:12]}"


def _smoke_attempt(package: JobPackage) -> str:
    """The suffix of a retried smoke job's id ('' for the first run), which also names its result prefix."""
    return (package.smoke_job_id or "").removeprefix(_smoke_base_id(package)).lstrip("-")


async def start_smoke(package: JobPackage, attempt: str = "") -> JobPackage:
    """Submit the package's smoke job (once: a package that has one is returned as it is). A retry passes an `attempt`
    suffix because Ray rejects a reused submission id."""
    if package.smoke_job_id is not None:
        return package
    name, job_id = package.name, "-".join(filter(None, (_smoke_base_id(package), attempt)))
    spec = {"job_id": job_id, "bucket": settings.data_plane.staging_bucket, "prefix": layout.smoke_prefix(name, package.sha256, attempt)}
    metadata = {"package": name, "sha256": package.sha256}
    core, bundles = package, []
    if package.kind == PackageKind.TASK:  # a bundle runs its own smoke on the stable core
        core, bundles = await resolve_stable(CORE), [package]
    elif name == CORE:  # a core runs every stable bundle's smoke: the core x bundle gate of planning/25
        async with (await get_pool()).acquire() as conn:
            bundles = await job_packages.list_kind(conn, PackageKind.TASK, PackageStatus.STABLE)
    requirements = job_packages.merged_requirements(core, *bundles)  # what a real run on these packages installs
    if requirements:
        metadata["requirements_sha256"] = requirements_sha256(requirements)
    if core.name == CORE:  # smoke_all: none stable yet = nothing to break, the core passes
        spec["bundles"] = [{"smoke": b.manifest["smoke"], "requires": b.manifest["requires"]} for b in bundles]
        metadata["core_sha256"] = core.sha256
    await job_submit.submit(
        package_url=await presigned_url(core.name, core.sha256), bundle_urls=[await presigned_url(b.name, b.sha256) for b in bundles],
        module=job_catalog.SMOKE_ENTRYPOINTS[core.name], spec_json=json.dumps(spec), job_id=job_id, metadata=metadata,
        num_cpus=settings.training.driver_cpus, timeout_seconds=SMOKE_TIMEOUT_SECONDS, requirements=requirements)
    async with (await get_pool()).acquire() as conn:
        await job_packages.start_smoke(conn, name, package.sha256, job_id)
        return await job_packages.get(conn, name, package.sha256)


async def _rerun_smoke(name: JobPackageName, sha256: str, was: SmokeStatus) -> JobPackage | None:
    """Start a new smoke job for a candidate whose smoke is `was`; None when it is no longer in that state. When Ray refuses
    the job the package goes back to failed, so the run can be repeated."""
    async with (await get_pool()).acquire() as conn:
        reset = await job_packages.reset_smoke(conn, name, sha256, was)
    if reset is None:
        return None
    try:
        return await start_smoke(reset, datetime.now(UTC).strftime(SMOKE_ATTEMPT_FORMAT))
    except Exception:
        async with (await get_pool()).acquire() as conn:
            await job_packages.set_smoke(conn, name, sha256, SmokeStatus.FAILED)
        raise


async def retry_smoke(name: JobPackageName, sha256: str) -> JobPackage | None:
    """Smoke a candidate whose smoke failed again; None when it is no longer in that state."""
    return await _rerun_smoke(name, sha256, SmokeStatus.FAILED)


async def restart_smoke(package: JobPackage) -> JobPackage:
    """Smoke a candidate whose smoke passed again, from now: a core is smoked against the bundle targets that are stable when
    its smoke starts, so one whose bundle targets moved during its smoke must be smoked again before it can move."""
    restarted = await _rerun_smoke(package.name, package.sha256, SmokeStatus.PASSED)
    return package if restarted is None else restarted


async def refresh_smoke(package: JobPackage) -> JobPackage:
    """Settle a pending smoke from what the job left: its smoke.json, else a dead Ray job means failed, else still pending."""
    if package.smoke_status != SmokeStatus.PENDING or package.smoke_job_id is None:
        return package
    written = await asyncio.to_thread(package_store.read, layout.smoke_prefix(package.name, package.sha256, _smoke_attempt(package)) + layout.SMOKE_KEY)
    if written is not None:
        outcome = SmokeStatus.PASSED if json.loads(written)["ok"] else SmokeStatus.FAILED
    else:
        try:
            outcome = SmokeStatus.FAILED if await compute.status(package.smoke_job_id) in _SMOKE_DEAD else None
        except httpx.HTTPError:  # the job is gone or Ray is unreachable: not an answer
            outcome = None
    if outcome is None:
        return package
    async with (await get_pool()).acquire() as conn:
        return await job_packages.set_smoke(conn, package.name, package.sha256, outcome)


async def retry_failed_smoke(package: JobPackage, detail: str) -> JobPackage:
    """Smoke a candidate whose smoke failed again (after the core or the image was fixed); anything else, or a retry another
    request won, is a 409 `detail`."""
    not_retryable = Problem(HTTPStatus.CONFLICT, SmokeProblem.NOT_RETRYABLE, detail)
    if package.status != PackageStatus.CANDIDATE or (await refresh_smoke(package)).smoke_status != SmokeStatus.FAILED:
        raise not_retryable
    retried = await retry_smoke(package.name, package.sha256)
    if retried is None:
        raise not_retryable
    return retried


async def resolve_stable(name: JobPackageName) -> JobPackage:
    """The package a new job runs: the newest stable one. Outside prod the bundled source is published and promoted first,
    through the same audited promotion a target move uses, so the previous stable is retired."""
    if settings.env != PROD_ENV:
        package = await publish(name, SELF_PUBLISHER)
        if package.status == PackageStatus.CANDIDATE:
            async with (await get_pool()).acquire() as conn:
                await job_packages.promote(conn, name, package.sha256, SELF_PUBLISHER)
    async with (await get_pool()).acquire() as conn:
        stable = await job_packages.latest_stable(conn, name)
    if stable is None:
        raise Problem(HTTPStatus.SERVICE_UNAVAILABLE, NO_STABLE_PACKAGE, f"no stable {name} package is published")
    return stable


async def presigned_url(name: JobPackageName, sha256: str, ttl_seconds: int | None = None) -> str:
    """A GET the Ray job can reach (signed for the endpoint of the `ingest` credential, which is the address Ray uses)
    for the registered package, whatever its status: a run keeps the package it was created with."""
    async with (await get_pool()).acquire() as conn:
        package = await job_packages.get(conn, name, sha256)
    if package is None:
        raise LookupError(f"{name} package {sha256} is not registered")
    return lakefs.s3_client(secret_files.INGEST).generate_presigned_url(
        "get_object", Params={"Bucket": settings.data_plane.staging_bucket, "Key": package.s3_key},
        ExpiresIn=ttl_seconds or settings.data_plane.presign_ttl_seconds)
