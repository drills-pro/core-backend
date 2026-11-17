"""The tabular encoder and postprocess: one row in the trainer's column order, and the probabilities best first."""
import numpy as np
import pytest

from ray_serving.inputs import tabular_encoder
from ray_serving.postprocess import ranked_probabilities

PRE = {"features": ["age", "income", "tenure"], "missing": "nan"}


def test_a_body_becomes_one_row_in_the_trainers_column_order():
    feeds, width, height = tabular_encoder(PRE, "", "features")(
        {"tenure": 3, "age": 40, "income": 5.5})
    assert list(feeds) == ["features"] and feeds["features"].dtype == np.float32
    assert feeds["features"].tolist() == [[40.0, 5.5, 3.0]] and (width, height) == (0, 0)


def test_a_missing_or_null_value_is_nan_as_it_was_at_training():
    feeds, _, _ = tabular_encoder(PRE, "", "features")({"age": None, "income": 1})
    row = feeds["features"][0]
    assert np.isnan(row[0]) and row[1] == 1.0 and np.isnan(row[2])


def test_a_value_that_is_not_a_number_is_refused():
    with pytest.raises(ValueError):
        tabular_encoder(PRE, "", "features")({"age": "old", "income": 1, "tenure": 2})


def test_probabilities_come_back_best_first_with_their_class_names():
    labels = np.array([1])
    probabilities = np.array([[0.2, 0.7, 0.1]], dtype=np.float32)
    out = ranked_probabilities([labels, probabilities], ["no", "yes", "maybe"], 0, 0, {})
    assert out == [{"class": "yes", "probability": pytest.approx(0.7)},
                   {"class": "no", "probability": pytest.approx(0.2)},
                   {"class": "maybe", "probability": pytest.approx(0.1)}]
