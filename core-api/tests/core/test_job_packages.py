"""job_packages: the enums mirror the migration's CHECKs."""
import asyncio
import re

from app.core import job_packages
from app.core.enums import JobPackageName, PackageAuditAction, PackageKind, PackageStatus, SmokeStatus
from app.core.job_packages import TASK_PACKAGE_PATTERN, task_package_name
from tests.support.check_mirror import assert_mirrors, assert_pattern_mirrors
from tests.support.packages import PackageDb


def test_the_enums_mirror_the_checks():
    # the name check lists the runtime names (the enum) beside the task-name pattern
    assert_mirrors("job_packages", "name", JobPackageName)
    assert_pattern_mirrors("job_packages", "name", TASK_PACKAGE_PATTERN)
    assert_mirrors("job_packages", "kind", PackageKind)
    assert_mirrors("job_package_audit", "action", PackageAuditAction)
    assert_mirrors("job_packages", "status", PackageStatus)


def test_a_task_package_name_matches_the_pattern():
    assert re.fullmatch(TASK_PACKAGE_PATTERN, task_package_name("image-classification"))


def test_the_smoke_enum_mirrors_its_check():
    assert_mirrors("job_packages", "smoke_status", SmokeStatus)


# ---- the lifecycle: every move that changes what runs is audited, in the transaction of the change ----

NAME = JobPackageName.RAY_JOBS
A, B = "a" * 64, "b" * 64


def _registered(*shas) -> PackageDb:
    db = PackageDb()
    for sha in shas:
        asyncio.run(job_packages.register(db, NAME, sha, f"jobs/{sha}.zip", "ci"))
    return db


def _statuses(db) -> dict[str, PackageStatus]:
    return {sha: row["status"] for (_, sha), row in db.rows.items()}


def test_a_new_package_is_audited_as_registered_and_a_known_one_is_not():
    db = _registered(A, A)
    assert db.audit == [(NAME, A, PackageAuditAction.REGISTER, "ci")] and db.rows[(NAME, A)]["kind"] == PackageKind.RUNTIME


def test_a_manifest_makes_the_package_a_task_bundle():
    db = PackageDb()
    package = asyncio.run(job_packages.register(db, "task_x", A, "k", "ci", ["numpy==2.1.0"], {"slug": "x"}))
    assert (package.kind, package.manifest, package.requirements) == (PackageKind.TASK, {"slug": "x"}, ["numpy==2.1.0"])


def test_promoting_leaves_one_stable_and_audits_it():
    db = _registered(A, B)
    asyncio.run(job_packages.promote(db, NAME, A, "ops"))
    asyncio.run(job_packages.promote(db, NAME, B, "ops"))
    assert _statuses(db) == {A: PackageStatus.RETIRED, B: PackageStatus.STABLE}
    assert db.audit[2:] ==[(NAME, A, PackageAuditAction.PROMOTE, "ops"), (NAME, B, PackageAuditAction.PROMOTE, "ops")]


def test_retiring_audits_it_and_an_unknown_package_leaves_no_trail():
    db = _registered(A)
    assert asyncio.run(job_packages.retire(db, NAME, A, "ops")).status == PackageStatus.RETIRED
    assert db.audit[-1] == (NAME, A, PackageAuditAction.RETIRE, "ops")
    trail = list(db.audit)
    assert asyncio.run(job_packages.retire(db, NAME, B, "ops")) is None and asyncio.run(job_packages.promote(db, NAME, B, "ops")) is None
    assert db.audit == trail


def test_promoting_an_unknown_package_leaves_the_stable_one_alone():
    db = _registered(A)
    asyncio.run(job_packages.promote(db, NAME, A, "ops"))
    trail = list(db.audit)
    assert asyncio.run(job_packages.promote(db, NAME, B, "ops")) is None
    assert _statuses(db) == {A: PackageStatus.STABLE} and db.audit == trail
