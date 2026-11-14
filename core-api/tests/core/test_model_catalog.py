"""model_catalog: the enums mirror the migration's CHECKs, and the seed stays inside the licence allowlist."""
import re
from pathlib import Path

import pytest

from app.core.enums import CatalogSource, CatalogStatus
from app.core.settings import settings

MIGRATION = next((Path(__file__).parents[2] / "migrations").glob("*_model_catalog.sql")).read_text()


def _check(constraint: str) -> set[str]:
    clause = re.search(rf"CONSTRAINT {constraint} CHECK \(\w+ IN \(([^)]*)\)\)", MIGRATION).group(1)
    return set(re.findall(r"'([^']+)'", clause))


@pytest.mark.parametrize("constraint, enum", [("model_catalog_status_check", CatalogStatus), ("model_catalog_source_check", CatalogSource)])
def test_the_checks_mirror_the_enums(constraint, enum):
    assert _check(constraint) == {e.value for e in enum}


def test_the_seeded_licences_are_on_the_allowlist():
    seeded = set(re.findall(r"THEN '([^']+)' ELSE '([^']+)'", MIGRATION)[0])
    assert seeded <= set(settings.training.model_license_allowlist)
