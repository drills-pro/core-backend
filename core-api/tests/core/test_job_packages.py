"""job_packages: the enums mirror the migration's CHECKs."""
import re
from pathlib import Path

from app.core.enums import JobPackageName, PackageStatus, SmokeStatus

MIGRATION = next((Path(__file__).parents[2] / "migrations").glob("*_job_packages.sql")).read_text()
SMOKE_MIGRATION = next((Path(__file__).parents[2] / "migrations").glob("*_job_packages_smoke.sql")).read_text()


def _in_list(column: str) -> set[str]:
    return set(re.search(rf"{column} text .*?CHECK \({column} IN \(([^)]*)\)", MIGRATION).group(1).replace("'", "").replace(" ", "").split(","))


def test_the_enums_mirror_the_checks():
    assert _in_list("name") == {n.value for n in JobPackageName}
    assert _in_list("status") == {s.value for s in PackageStatus}


def test_the_smoke_enum_mirrors_its_check():
    listed = re.search(r"smoke_status text .*?CHECK \(smoke_status IN \(([^)]*)\)", SMOKE_MIGRATION).group(1).replace("'", "").replace(" ", "").split(",")
    assert set(listed) == {s.value for s in SmokeStatus}
