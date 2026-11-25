"""The target lifecycle: CI's target call and the poller that moves targets, with the rows and the data plane faked, so each rule
is read off the outcome: which version became the target, and which one was smoked again instead."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.enums import JobPackageName, PackageKind, PackageStatus, SmokeStatus
from app.core.job_packages import JobPackage
from app.modules.admin.job_packages import service
from app.modules.admin.job_packages.constants import AUTO_PROMOTER
from tests.support import TenantCtx
from tests.support.bundles import SLUG, manifest
from tests.support.packages import package_row

T0 = datetime(2026, 11, 1, tzinfo=UTC)
SHA_A, SHA_B = "a" * 64, "b" * 64


def pkg(name, sha, *, at=0, status=PackageStatus.CANDIDATE, smoke=SmokeStatus.PASSED):
    """A package row `at` minutes after T0 (its updated_at), with its smoke settled as given."""
    row = package_row(name, sha, f"jobs/{name}-{sha}.zip", "ci", status) | {
        "smoke_status": smoke, "updated_at": T0 + timedelta(minutes=at)}
    if name.startswith("task_"):
        row |= {"kind": PackageKind.TASK, "manifest": manifest(slug=SLUG)}
    return JobPackage.model_validate(row)


def _pool(monkeypatch):
    async def get_pool():
        return SimpleNamespace(acquire=lambda: TenantCtx(None))

    monkeypatch.setattr(service, "get_pool", get_pool)


@pytest.fixture
def poll(monkeypatch):
    """Run promote_targets over the given rows; returns (moved, restarted): which candidates became a target, and which core
    was smoked again instead."""

    def run(candidates=(), stable=(), bundles=(), bundles_allowed=True):
        moved, restarted = [], []

        async def list_status(conn, status):
            return list(candidates if status == PackageStatus.CANDIDATE else stable)

        async def list_kind(conn, kind, status=None):
            return list(bundles)

        async def refresh(package):
            return package

        async def promote(package, actor):
            moved.append((package.name, package.sha256, actor))
            return package.model_copy(update={"status": PackageStatus.STABLE})

        async def restart(package):
            restarted.append(package.sha256)
            return package

        async def allowed(slug):
            return bundles_allowed

        _pool(monkeypatch)
        monkeypatch.setattr(service.job_packages, "list_status", list_status)
        monkeypatch.setattr(service.job_packages, "list_kind", list_kind)
        monkeypatch.setattr(service.data_plane, "refresh_package_smoke", refresh)
        monkeypatch.setattr(service.data_plane, "restart_package_smoke", restart)
        monkeypatch.setattr(service, "_promote", promote)
        monkeypatch.setattr(service.task_bundles, "auto_promote_allowed", allowed)
        asyncio.run(service.promote_targets())
        return moved, restarted

    return run


def test_the_newest_passed_candidate_becomes_the_target_when_there_is_none(poll):
    assert poll([pkg("ray_jobs", SHA_A, at=1)]) == ([("ray_jobs", SHA_A, AUTO_PROMOTER)], [])


def test_a_candidate_pushed_before_the_current_target_does_not_replace_it(poll):
    assert poll([pkg("ray_jobs", SHA_A, at=1)], stable=[pkg("ray_jobs", SHA_B, at=5, status=PackageStatus.STABLE)]) == ([], [])


def test_a_candidate_pushed_after_the_current_target_replaces_it(poll):
    moved, _ = poll([pkg("ray_jobs", SHA_A, at=5)], stable=[pkg("ray_jobs", SHA_B, at=1, status=PackageStatus.STABLE)])
    assert moved == [("ray_jobs", SHA_A, AUTO_PROMOTER)]


def test_of_two_passing_candidates_the_newer_one_ends_as_the_target(poll):
    moved, _ = poll([pkg("ray_jobs", SHA_A, at=1), pkg("ray_jobs", SHA_B, at=2)])
    assert moved == [("ray_jobs", SHA_A, AUTO_PROMOTER), ("ray_jobs", SHA_B, AUTO_PROMOTER)]


def test_a_candidate_whose_smoke_has_not_settled_or_failed_waits(poll):
    assert poll([pkg("ray_jobs", SHA_A, at=1, smoke=SmokeStatus.PENDING), pkg("ray_jobs", SHA_B, at=2, smoke=SmokeStatus.FAILED)]) == ([], [])


def test_a_task_bundle_waits_while_its_slug_opted_out_or_the_switch_is_off(poll):
    bundle = pkg(f"task_{SLUG}", SHA_A, at=1)
    assert poll([bundle], bundles_allowed=False) == ([], [])
    assert poll([bundle], bundles_allowed=True) == ([(f"task_{SLUG}", SHA_A, AUTO_PROMOTER)], [])


def test_the_core_is_smoked_again_when_a_bundle_target_moved_during_its_smoke(poll):
    core = pkg(JobPackageName.RAY_TRAINING, SHA_A, at=1)
    bundle_moved = pkg(f"task_{SLUG}", SHA_B, at=3, status=PackageStatus.STABLE)
    assert poll([core], bundles=[bundle_moved]) == ([], [SHA_A])


def test_the_core_moves_when_its_bundle_targets_have_not_moved_since_its_smoke(poll):
    core = pkg(JobPackageName.RAY_TRAINING, SHA_A, at=3)
    bundle_before = pkg(f"task_{SLUG}", SHA_B, at=1, status=PackageStatus.STABLE)
    moved, restarted = poll([core], bundles=[bundle_before])
    assert moved == [("ray_training", SHA_A, AUTO_PROMOTER)] and restarted == []


@pytest.fixture
def target(monkeypatch):
    """service.target over a registered row: the data plane answers with `row` (its smoke as given), and promotions are recorded."""

    def run(name, row, stable=None, bundles=()):
        moved, retried, restarted = [], [], []

        async def register(*args):
            return row

        async def start(package, attempt=""):
            return package

        async def refresh(package):
            return package

        async def retry(package, detail):
            retried.append(package.sha256)
            return package

        async def restart(package):
            restarted.append(package.sha256)
            return package

        async def promote(package, actor):
            moved.append((package.name, package.sha256, actor))
            return package.model_copy(update={"status": PackageStatus.STABLE})

        async def list_kind(conn, kind, status=None):
            return list(bundles)

        _pool(monkeypatch)
        monkeypatch.setattr(service.data_plane, "register_staged_package", register)
        monkeypatch.setattr(service.data_plane, "register_staged_bundle", register)
        monkeypatch.setattr(service.data_plane, "start_package_smoke", start)
        monkeypatch.setattr(service.data_plane, "refresh_package_smoke", refresh)
        monkeypatch.setattr(service.data_plane, "retry_failed_package_smoke", retry)
        monkeypatch.setattr(service.data_plane, "restart_package_smoke", restart)
        monkeypatch.setattr(service.job_packages, "list_kind", list_kind)
        monkeypatch.setattr(service, "_promote", promote)
        result = asyncio.run(service.target(name, f"jobs/{name}-{row.sha256}.zip", "ops"))
        return result, SimpleNamespace(moved=moved, retried=retried, restarted=restarted)

    return run


def test_a_new_version_whose_smoke_is_running_waits_for_the_poller(target):
    result, seen = target("ray_jobs", pkg("ray_jobs", SHA_A, smoke=SmokeStatus.PENDING))
    assert result.status == PackageStatus.CANDIDATE and seen.moved == [] and seen.retried == []


def test_a_rollback_to_a_version_that_passed_moves_it_even_though_it_is_older(target):
    result, seen = target("ray_jobs", pkg("ray_jobs", SHA_A, at=1, status=PackageStatus.RETIRED))
    assert seen.moved == [("ray_jobs", SHA_A, "ops")] and result.status == PackageStatus.STABLE


def test_a_version_that_is_already_the_target_is_returned_as_it_is(target):
    result, seen = target("ray_jobs", pkg("ray_jobs", SHA_A, status=PackageStatus.STABLE))
    assert result.status == PackageStatus.STABLE and seen.moved == [] and seen.retried == []


def test_a_version_whose_smoke_failed_is_smoked_again(target):
    result, seen = target("ray_jobs", pkg("ray_jobs", SHA_A, smoke=SmokeStatus.FAILED))
    assert seen.retried == [SHA_A] and seen.moved == []


def test_a_core_whose_bundle_targets_moved_is_smoked_again_instead_of_moving(target):
    core = pkg(JobPackageName.RAY_TRAINING, SHA_A, at=1, status=PackageStatus.RETIRED)
    bundle = pkg(f"task_{SLUG}", SHA_B, at=3, status=PackageStatus.STABLE)
    _, seen = target("ray_training", core, bundles=[bundle])
    assert seen.restarted == [SHA_A] and seen.moved == []


def test_a_bundle_whose_smoke_passed_moves_to_its_task_target(target):
    _, seen = target(f"task_{SLUG}", pkg(f"task_{SLUG}", SHA_A))
    assert seen.moved == [(f"task_{SLUG}", SHA_A, "ops")]
