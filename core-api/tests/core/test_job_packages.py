"""job_packages: the enums mirror the migration's CHECKs."""
import asyncio
import re
from pathlib import Path

from app.core import job_packages
from app.core.enums import JobPackageName, PackageAuditAction, PackageKind, PackageStatus, SmokeStatus
from app.core.job_packages import TASK_PACKAGE_PATTERN, task_package_name
from tests.support.packages import PackageDb

MIGRATIONS = Path(__file__).parents[2] / "migrations"
MIGRATION = next(MIGRATIONS.glob("*_job_packages.sql")).read_text()
SHARED_SERVING = next(MIGRATIONS.glob("*_shared_serving.sql")).read_text()
SMOKE_MIGRATION = next((Path(__file__).parents[2] / "migrations").glob("*_job_packages_smoke.sql")).read_text()
BUNDLES = next(MIGRATIONS.glob("*_task_bundles.sql")).read_text().split("-- migrate:down")[0]


def _listed(pattern: str, sql: str) -> set[str]:
    return set(re.search(pattern, sql, re.S).group(1).replace("'", "").replace(" ", "").split(","))


def _in_list(column: str) -> set[str]:
    return _listed(rf"{column} text .*?CHECK \({column} IN \(([^)]*)\)", MIGRATION)


def test_the_enums_mirror_the_checks():
    # the newest migration that redefines the name check: runtime names are the enum, task names the pattern
    assert _listed(r"kind = 'runtime' AND name IN \(([^)]*)\)", BUNDLES) == {n.value for n in JobPackageName}
    assert f"kind = 'task' AND name ~ '{TASK_PACKAGE_PATTERN}'" in BUNDLES
    assert _listed(r"job_packages_kind_check CHECK \(kind IN \(([^)]*)\)", BUNDLES) == {k.value for k in PackageKind}
    assert _listed(r"job_package_audit_action_check CHECK \(action IN \(([^)]*)\)", BUNDLES) == {a.value for a in PackageAuditAction}
    assert _in_list("status") == {s.value for s in PackageStatus}


def test_a_task_package_name_matches_the_pattern():
    assert re.fullmatch(TASK_PACKAGE_PATTERN, task_package_name("image-classification"))


def test_the_smoke_enum_mirrors_its_check():
    listed = re.search(r"smoke_status text .*?CHECK \(smoke_status IN \(([^)]*)\)", SMOKE_MIGRATION).group(1).replace("'", "").replace(" ", "").split(",")
    assert set(listed) == {s.value for s in SmokeStatus}


def test_the_serving_enum_mirrors_the_deployments_check():
    from app.modules.user.deployments.constants import Serving

    listed = re.search(r"deployments_serving_check CHECK \(serving IN \(([^)]*)\)", SHARED_SERVING).group(1).replace("'", "").replace(" ", "").split(",")
    assert set(listed) == {s.value for s in Serving}


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
