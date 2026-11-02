"""job_packages: the enums mirror the migration's CHECKs."""
import re
from pathlib import Path

from app.core.enums import JobPackageName, PackageStatus

MIGRATION = next((Path(__file__).parents[2] / "migrations").glob("*_job_packages.sql")).read_text()


def _in_list(column: str) -> set[str]:
    return set(re.search(rf"{column} text .*?CHECK \({column} IN \(([^)]*)\)", MIGRATION).group(1).replace("'", "").replace(" ", "").split(","))


def test_the_enums_mirror_the_checks():
    assert _in_list("name") == {n.value for n in JobPackageName}
    assert _in_list("status") == {s.value for s in PackageStatus}
