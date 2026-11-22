"""The prediction enums equal the CHECK constraints that mirror them, read from the migration files (no database needed)."""
import pytest

from app.modules.user.deployments.constants import PredictionMode, PredictionStatus

from tests.support.check_mirror import assert_mirrors

@pytest.mark.parametrize(
    ("enum", "table", "column"),
    [
        (PredictionStatus, "predictions", "status"),
        (PredictionStatus, "prediction_events", "status"),
        (PredictionMode, "predictions", "mode"),
    ],
)
def test_the_enum_matches_its_check_constraint(enum, table, column):
    assert_mirrors(table, column, enum)
