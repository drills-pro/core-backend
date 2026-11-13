"""Task bundles in staging: what register accepts, and how a bundle's smoke runs on the core."""
import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from app.core.enums import JobPackageName, PackageKind, PackageStatus
from app.core.job_packages import JobPackage
from app.core.problem import Problem
from app.integrations.data_plane import job_catalog, job_package, task_bundle
from tests.integrations.data_plane.test_job_package import package_row
from tests.support import TenantCtx
from tests.support.bundles import SLUG, bundle_zip, manifest

CORE_SHA, OTHER = "a" * 64, "b" * 64


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_a_good_bundle_gives_its_manifest():
    data = bundle_zip()
    assert task_bundle.check_bundle(data, _sha(data)).slug == SLUG


@pytest.mark.parametrize("data, code", [
    (bundle_zip(extra={"package/imgcls/model.bin": b"x"}), "invalid_package"),
    (bundle_zip(extra={"outside.py": b""}), "invalid_package"),
    (bundle_zip(manifest(contract=2)), "invalid_manifest"),
    (bundle_zip(manifest(trainer="imgcls.train:nope")), "invalid_manifest"),
    (bundle_zip(manifest(data_kind="vision")), "invalid_manifest"),
    (b"not a zip", "invalid_package"),
])
def test_a_bad_bundle_is_refused(data, code):
    with pytest.raises(Problem) as e:
        task_bundle.check_bundle(data, _sha(data))
    assert e.value.status_code == 422 and e.value.code == code


def test_a_bundle_without_a_manifest_or_with_a_wrong_hash_is_refused():
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("package/imgcls/train.py", "")
    with pytest.raises(Problem, match="manifest"):
        task_bundle.check_bundle(buf.getvalue(), _sha(buf.getvalue()))
    with pytest.raises(Problem, match="hash"):
        task_bundle.check_bundle(bundle_zip(), OTHER)


@pytest.fixture
def staged(monkeypatch):
    """register_staged against a staged object and a recording register_bundle."""
    seen = []

    async def get_pool():
        return SimpleNamespace(acquire=lambda: TenantCtx(None))

    async def register_bundle(conn, name, sha, key, doc, actor):
        seen.append((name, sha, key, doc["schema"], actor))
        return JobPackage.model_validate(package_row(name, sha, key, actor) | {"kind": PackageKind.TASK, "manifest": doc})

    monkeypatch.setattr(task_bundle, "get_pool", get_pool)
    monkeypatch.setattr(task_bundle.job_packages, "register_bundle", register_bundle)

    def stage(data):
        monkeypatch.setattr(task_bundle, "_read_staged", lambda key: data)
        return f"jobs/{SLUG}-{_sha(data)}.zip"

    return stage, seen


def test_a_staged_bundle_is_registered_as_task_slug(staged):
    stage, seen = staged
    key = stage(bundle_zip())
    package = asyncio.run(task_bundle.register_staged(key, "ci"))
    assert seen == [(f"task_{SLUG}", package.sha256, key, 1, "ci")] and package.kind == PackageKind.TASK


@pytest.mark.parametrize("key", ["jobs/image-classification.zip", "other/image-classification-" + "a" * 64 + ".zip"])
def test_a_key_that_is_not_a_bundle_key_is_refused(staged, key):
    with pytest.raises(Problem, match="expected a key"):
        asyncio.run(task_bundle.register_staged(key, "ci"))


def test_the_key_slug_must_be_the_manifest_slug(staged):
    stage, seen = staged
    data = bundle_zip()
    stage(data)
    with pytest.raises(Problem, match="manifest"):
        asyncio.run(task_bundle.register_staged(f"jobs/object-detection-{_sha(data)}.zip", "ci"))
    assert seen == []


def test_a_bundle_smoke_runs_the_bundle_function_on_the_stable_core(monkeypatch):
    bundle = JobPackage.model_validate(
        package_row(f"task_{SLUG}", OTHER, "k", "ci") | {"kind": PackageKind.TASK, "manifest": manifest()})
    core = JobPackage.model_validate(package_row(JobPackageName.RAY_TRAINING, CORE_SHA, "c", "ci", PackageStatus.STABLE))
    sent = {}

    async def submit(**kw):
        sent.update(kw)

    async def url(name, sha):
        return f"http://ray/{name}/{sha}"

    async def resolve_core():
        return core

    async def get_pool():
        return SimpleNamespace(acquire=lambda: TenantCtx(None))

    async def noop(*a):
        return bundle

    monkeypatch.setattr(job_package.job_submit, "submit", submit)
    monkeypatch.setattr(job_package, "presigned_url", url)
    monkeypatch.setattr(job_package, "resolve_core", resolve_core)
    monkeypatch.setattr(job_package, "get_pool", get_pool)
    monkeypatch.setattr(job_package.job_packages, "start_smoke", noop)
    monkeypatch.setattr(job_package.job_packages, "get", noop)
    asyncio.run(job_package.start_smoke(bundle))
    assert sent["module"] == job_catalog.BUNDLE_SMOKE_ENTRYPOINT and json.loads(sent["spec_json"])["smoke"] == "imgcls.smoke:run"
    assert (sent["package_url"], sent["bundle_url"]) == (f"http://ray/ray_training/{CORE_SHA}", f"http://ray/task_{SLUG}/{OTHER}")
    assert sent["metadata"]["core_sha256"] == CORE_SHA


def test_the_core_is_the_stable_ray_core_else_ray_training(monkeypatch):
    core = JobPackage.model_validate(package_row(JobPackageName.RAY_CORE, CORE_SHA, "c", "ci", PackageStatus.STABLE))
    found = {}

    async def latest_stable(conn, name):
        return found.get(name)

    async def get_pool():
        return SimpleNamespace(acquire=lambda: TenantCtx(None))

    async def fallback(name):
        return f"stable {name}"

    monkeypatch.setattr(job_package, "get_pool", get_pool)
    monkeypatch.setattr(job_package.job_packages, "latest_stable", latest_stable)
    monkeypatch.setattr(job_package, "resolve_stable", fallback)
    assert asyncio.run(job_package.resolve_core()) == "stable ray_training"
    found[JobPackageName.RAY_CORE] = core
    assert asyncio.run(job_package.resolve_core()) is core


def test_a_runtime_name_with_no_source_is_422():
    with pytest.raises(Problem) as e:
        asyncio.run(job_package.register_staged(JobPackageName.RAY_CORE, "jobs/x.zip", "ci"))
    assert e.value.code == job_package.NO_PACKAGE_SOURCE
