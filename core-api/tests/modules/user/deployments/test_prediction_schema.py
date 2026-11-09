"""The prediction enums equal the CHECK constraints that mirror them, read from the migration files (no database needed)."""
import re
from pathlib import Path

import pytest

from app.modules.user.deployments.constants import PredictionMode, PredictionStatus

MIGRATIONS = "\n".join(
    p.read_text().split("-- migrate:down")[0] for p in sorted((Path(__file__).parents[4] / "migrations").glob("*.sql"))
)


def check_values(constraint: str) -> set[str]:
    found = re.findall(rf"CONSTRAINT {constraint}\s+CHECK \(\w+ IN \(([^)]*)\)\)", MIGRATIONS)
    assert found, f"no CHECK named {constraint} in the migrations"
    return set(re.findall(r"'([^']+)'", found[-1]))


@pytest.mark.parametrize(
    ("enum", "constraint"),
    [
        (PredictionStatus, "predictions_status_check"),
        (PredictionStatus, "prediction_events_status_check"),
        (PredictionMode, "predictions_mode_check"),
    ],
)
def test_the_enum_matches_its_check_constraint(enum, constraint):
    assert {m.value for m in enum} == check_values(constraint)
