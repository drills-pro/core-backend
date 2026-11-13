"""job_packages: the enums mirror the migration's CHECKs."""
import re
from pathlib import Path

from app.core.enums import JobPackageName, PackageAuditAction, PackageKind, PackageStatus, SmokeStatus
from app.core.job_packages import TASK_PACKAGE_PATTERN, task_package_name

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
    assert not re.fullmatch(TASK_PACKAGE_PATTERN, JobPackageName.RAY_CORE)


def test_the_smoke_enum_mirrors_its_check():
    listed = re.search(r"smoke_status text .*?CHECK \(smoke_status IN \(([^)]*)\)", SMOKE_MIGRATION).group(1).replace("'", "").replace(" ", "").split(",")
    assert set(listed) == {s.value for s in SmokeStatus}


def test_the_serving_enum_mirrors_the_deployments_check():
    from app.modules.user.deployments.constants import Serving

    listed = re.search(r"deployments_serving_check CHECK \(serving IN \(([^)]*)\)", SHARED_SERVING).group(1).replace("'", "").replace(" ", "").split(",")
    assert set(listed) == {s.value for s in Serving}
