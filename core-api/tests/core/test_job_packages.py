"""job_packages: the enums mirror the migration's CHECKs."""
import re
from pathlib import Path

from app.core.enums import JobPackageName, PackageStatus, SmokeStatus

MIGRATIONS = Path(__file__).parents[2] / "migrations"
MIGRATION = next(MIGRATIONS.glob("*_job_packages.sql")).read_text()
SHARED_SERVING = next(MIGRATIONS.glob("*_shared_serving.sql")).read_text()
SMOKE_MIGRATION = next((Path(__file__).parents[2] / "migrations").glob("*_job_packages_smoke.sql")).read_text()


def _in_list(column: str) -> set[str]:
    return set(re.search(rf"{column} text .*?CHECK \({column} IN \(([^)]*)\)", MIGRATION).group(1).replace("'", "").replace(" ", "").split(","))


def test_the_enums_mirror_the_checks():
    listed = re.search(r"job_packages_name_check CHECK \(name IN \(([^)]*)\)", SHARED_SERVING).group(1).replace("'", "").replace(" ", "").split(",")
    assert set(listed) == {n.value for n in JobPackageName}  # the newest migration that redefines the check
    assert _in_list("status") == {s.value for s in PackageStatus}


def test_the_smoke_enum_mirrors_its_check():
    listed = re.search(r"smoke_status text .*?CHECK \(smoke_status IN \(([^)]*)\)", SMOKE_MIGRATION).group(1).replace("'", "").replace(" ", "").split(",")
    assert set(listed) == {s.value for s in SmokeStatus}


def test_the_serving_enum_mirrors_the_deployments_check():
    from app.modules.user.deployments.constants import Serving

    listed = re.search(r"deployments_serving_check CHECK \(serving IN \(([^)]*)\)", SHARED_SERVING).group(1).replace("'", "").replace(" ", "").split(",")
    assert set(listed) == {s.value for s in Serving}
