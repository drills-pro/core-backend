"""An XGBoost model exported the way the tabular bundles export it is served by the shared predictor: ONNX in onnxruntime, the encoder
and postprocess named by the task type. Skipped where xgboost or onnxmltools is not installed."""
import json

import numpy as np
import pytest

from ray_serving import model

xgboost = pytest.importorskip("xgboost")
pytest.importorskip("onnxruntime")
convert = pytest.importorskip("onnxmltools")

FEATURES = ["age", "colour"]
CATEGORIES = {"colour": ["blue", "red"]}
ONNX_OPSET = 15  # the bundles' own


def served(tmp_path, fitted, postprocess, class_names):
    from onnxmltools.convert.common.data_types import FloatTensorType

    onnx = convert.convert_xgboost(fitted, initial_types=[("features", FloatTensorType([None, len(FEATURES)]))], target_opset=ONNX_OPSET)
    (tmp_path / "model.onnx").write_bytes(onnx.SerializeToString())
    (tmp_path / "model.json").write_text(json.dumps({"preprocessing": {"features": FEATURES, "missing": "nan", "categories": CATEGORIES}}))
    spec = model.ModelSpec(
        (tmp_path / "model.onnx").as_uri(), (tmp_path / "model.json").as_uri(), "", class_names, {}, ["CPUExecutionProvider"],
        "tabular_encoder", postprocess)
    return model.load(spec, tmp_path.as_uri())


def data(rows=200):
    rng = np.random.default_rng(0)
    age = rng.uniform(0, 10, rows)
    colour = rng.integers(0, 2, rows)  # 0 = blue, 1 = red
    return np.column_stack([age, colour]).astype(np.float32), age, colour


@pytest.mark.parametrize("classes", [["no", "yes"], ["a", "b", "c"]])  # binary and multi-class
def test_a_classifier_is_served_with_ranked_probabilities(tmp_path, classes):
    x, age, colour = data()
    y = np.where(colour == 1, len(classes) - 1, np.minimum(age // 4, len(classes) - 2)).astype(int)
    fitted = xgboost.XGBClassifier(n_estimators=30, max_depth=3, tree_method="hist").fit(x, y)
    loaded = served(tmp_path, fitted, "ranked_probabilities", classes)
    answer = model.predict(loaded, {"age": 1.0, "colour": "red"})
    assert [p["class"] for p in answer][0] == classes[-1] and sum(p["probability"] for p in answer) == pytest.approx(1, abs=1e-4)
    assert len(answer) == len(classes)
    assert model.predict(loaded, {"age": 1.0, "colour": "purple"})  # a colour never trained on is missing, not an error
    assert model.predict(loaded, {})  # an empty row is all missing


def test_a_regressor_is_served_as_one_value(tmp_path):
    x, age, colour = data()
    fitted = xgboost.XGBRegressor(n_estimators=60, max_depth=3, tree_method="hist").fit(x, 10 * age + 100 * colour)
    loaded = served(tmp_path, fitted, "regress", [])
    (answer,) = model.predict(loaded, {"age": 5, "colour": "red"})
    assert answer["value"] == pytest.approx(150, abs=15)
    with pytest.raises(model.ServeError) as err:
        model.predict(loaded, {"age": "old"})
    assert err.value.code == model.ErrorCode.BAD_INPUT
