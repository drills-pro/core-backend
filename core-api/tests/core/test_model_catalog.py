"""model_catalog: the enums mirror the migration's CHECKs, and the seed stays inside the licence allowlist."""
import re
from pathlib import Path

import pytest

from app.core.enums import CatalogSource, CatalogStatus
from app.core.settings import settings
from tests.support.check_mirror import assert_mirrors

MIGRATION = next((Path(__file__).parents[2] / "migrations").glob("*_model_catalog.sql")).read_text()


@pytest.mark.parametrize("column, enum", [("status", CatalogStatus), ("source", CatalogSource)])
def test_the_checks_mirror_the_enums(column, enum):
    assert_mirrors("model_catalog", column, enum)


def test_the_seeded_licences_are_on_the_allowlist():
    seeded = set(re.findall(r"THEN '([^']+)' ELSE '([^']+)'", MIGRATION)[0])
    assert seeded <= set(settings.training.model_license_allowlist)
