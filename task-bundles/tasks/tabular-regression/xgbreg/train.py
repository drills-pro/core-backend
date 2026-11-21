"""Tabular regression: XGBoost on a tabular dataset's data.parquet, exported to ONNX (onnxmltools).

Same table and features as tabular classification (ray_core.tabular: numeric and bool columns, and string columns with few values
as categories recorded in model.json); the target column is the `target` hyperparameter and must hold numbers. The model answers
one number. Imports nothing of the platform but ray_core.
"""
from pathlib import Path

import numpy as np

from ray_core import loop
from ray_core.constants import ROOT_RANK
from ray_core.data import Get, read_index
from ray_core.errors import BadData
from ray_core.metrics import regression_metrics
from ray_core.result import Result
from ray_core.spec import TrainSpec
from ray_core.tabular import features_of, matrix, split_rows

INPUT_NAME = "features"
OUTPUT_NAME = "variable"  # the name onnxmltools gives the regressor's one output, shape (batch, 1)
ONNX_OPSET = 15  # the newest the onnxmltools XGBoost converter targets reliably
DEFAULTS = {"target": "label", "n_estimators": 200, "max_depth": 6}  # mirror manifest.json's hyperparameter defaults


def export(model, n_features: int, path: Path) -> None:
    from onnxmltools import convert_xgboost
    from onnxmltools.convert.common.data_types import FloatTensorType

    onnx = convert_xgboost(model, initial_types=[(INPUT_NAME, FloatTensorType([None, n_features]))], target_opset=ONNX_OPSET)
    path.write_bytes(onnx.SerializeToString())


def regressor(xgboost, spec: TrainSpec, hp: dict, trees: int, rounds: int | None):
    return xgboost.XGBRegressor(
        n_estimators=trees, max_depth=hp["max_depth"], learning_rate=spec.learning_rate, random_state=spec.seed,
        tree_method="hist", early_stopping_rounds=rounds)


def is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def train(spec: TrainSpec, work: Path, get: Get) -> Result | None:
    import xgboost

    if loop.session.rank != ROOT_RANK:  # one XGBoost fit, not data parallel: another Ray Train worker has nothing to do
        return None
    hp = {**DEFAULTS, **spec.hyperparameters}
    target = hp["target"]
    rows = [r for r in read_index(get(spec.data_url)) if r.get(target) is not None]
    if not rows:
        raise BadData(f"data.parquet has no rows with a value in the target column {target!r}")
    if not all(is_number(r[target]) for r in rows):
        raise BadData(f"the target column {target!r} must hold numbers")
    features, categories = features_of(rows, target)
    if not features:
        raise BadData("data.parquet has no numeric, bool or category feature column")
    train_rows, held_out = split_rows(rows)
    y_train, y_held = (np.array([r[target] for r in part], dtype=np.float64) for part in (train_rows, held_out))
    x_train, x_held = matrix(train_rows, features, categories), matrix(held_out, features, categories)
    trees = hp["n_estimators"]
    if spec.early_stopping_patience:  # the rounds without a better held-out error, as epochs are for the neural tasks
        probe = regressor(xgboost, spec, hp, hp["n_estimators"], spec.early_stopping_patience)
        probe.fit(x_train, y_train, eval_set=[(x_held, y_held)], verbose=False)
        trees = probe.best_iteration + 1
    model = regressor(xgboost, spec, hp, trees, None)  # the export keeps every tree it is given: exactly the trees up to the best round
    model.fit(x_train, y_train)
    metrics = regression_metrics(model.predict(x_held), y_held)
    onnx = work / "model.onnx"
    export(model, len(features), onnx)
    signature = {
        "inputs": [{"name": INPUT_NAME, "shape": ["batch", len(features)], "dtype": "float32"}],
        "outputs": [{"name": OUTPUT_NAME, "shape": ["batch", 1], "dtype": "float32"}],
        "preprocessing": {"features": features, "target": target, "missing": "nan", "categories": categories}}
    return Result([], metrics, [{"epoch": 1, **metrics}], onnx, signature)
