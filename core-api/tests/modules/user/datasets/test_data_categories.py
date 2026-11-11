"""datasets.data_categories: the enum and the migration's CHECK list the same values; a create carries the declaration."""
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.modules.user.datasets.constants import DataCategory
from app.modules.user.datasets.schemas import DatasetCreate

MIGRATION = next(Path(__file__).parents[4].glob("migrations/*_datasets_data_categories.sql")).read_text()


def test_the_enum_matches_the_check_constraint():
    listed = set(re.findall(r"'(\w+)'", re.search(r"ARRAY\[(.*?)\]", MIGRATION).group(1)))
    assert listed == {c.value for c in DataCategory}


def test_a_create_declares_categories_and_rejects_unknown_ones():
    assert DatasetCreate(name="d", data_categories=["biometric"]).data_categories == [DataCategory.BIOMETRIC]
    assert DatasetCreate(name="d").data_categories == []
    with pytest.raises(ValidationError):
        DatasetCreate(name="d", data_categories=["secret"])
