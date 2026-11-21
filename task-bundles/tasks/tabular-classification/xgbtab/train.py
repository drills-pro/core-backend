"""Tabular classification: XGBoost on a tabular dataset's data.parquet, exported to ONNX (onnxmltools).

data.parquet is the CSV / Hugging Face table as ingest typed it (bool, int64, float64, string columns) plus the nullable `split`
column. The target column is the `target` hyperparameter. Rows without a split are assigned one from the hash of the row, like
images and text. Imports nothing of the platform but ray_core: this bundle is the proof that a task needs no core-backend code.

A string column with few distinct values is a category: it becomes one numeric feature, the index of its value in the sorted
values, and the values are recorded in model.json (`preprocessing.categories`) for serving to encode the same way. A value
the model never saw is missing, as at training. Any other string column (an id, free text) is dropped.
"""
from pathlib import Path

import numpy as np

from ray_core import loop
from ray_core.constants import ROOT_RANK
from ray_core.data import Get, read_index, require_classes
from ray_core.errors import BadData
from ray_core.metrics import classification_metrics
from ray_core.result import Result
from ray_core.spec import TrainSpec
from ray_core.tabular import features_of, matrix, split_rows

INPUT_NAME = "features"
OUTPUTS = ["label", "probabilities"]
ONNX_OPSET = 15  # the newest the onnxmltools XGBoost converter targets reliably
DEFAULTS = {"target": "label", "n_estimators": 200, "max_depth": 6}  # mirror manifest.json's hyperparameter defaults


def export(model, n_features: int, path: Path) -> None:
    from onnxmltools import convert_xgboost
    from onnxmltools.convert.common.data_types import FloatTensorType

    onnx = convert_xgboost(model, initial_types=[(INPUT_NAME, FloatTensorType([None, n_features]))], target_opset=ONNX_OPSET)
    path.write_bytes(onnx.SerializeToString())


def classifier(xgboost, spec: TrainSpec, hp: dict, trees: int, rounds: int | None):
    return xgboost.XGBClassifier(
        n_estimators=trees, max_depth=hp["max_depth"], learning_rate=spec.learning_rate, random_state=spec.seed,
        tree_method="hist", early_stopping_rounds=rounds)


def train(spec: TrainSpec, work: Path, get: Get) -> Result | None:
    import xgboost

    if loop.session.rank != ROOT_RANK:  # one XGBoost fit, not data parallel: another Ray Train worker has nothing to do
        return None
    hp = {**DEFAULTS, **spec.hyperparameters}
    target = hp["target"]
    rows = [r for r in read_index(get(spec.data_url)) if r.get(target) is not None]
    if not rows:
        raise BadData(f"data.parquet has no rows with a value in the target column {target!r}")
    features, categories = features_of(rows, target)
    if not features:
        raise BadData("data.parquet has no numeric, bool or category feature column")
    classes = sorted({str(r[target]) for r in rows})
    require_classes(classes)
    ids = {c: i for i, c in enumerate(classes)}
    train_rows, held_out = split_rows(rows)
    y_train, y_held = (np.array([ids[str(r[target])] for r in part]) for part in (train_rows, held_out))
    if len(set(y_train.tolist())) != len(classes):
        raise BadData("every class needs at least one training row")
    x_train, x_held = matrix(train_rows, features, categories), matrix(held_out, features, categories)
    trees = hp["n_estimators"]
    if spec.early_stopping_patience:  # the rounds without a better held-out logloss, as epochs are for the neural tasks
        probe = classifier(xgboost, spec, hp, hp["n_estimators"], spec.early_stopping_patience)
        probe.fit(x_train, y_train, eval_set=[(x_held, y_held)], verbose=False)
        trees = probe.best_iteration + 1
    model = classifier(xgboost, spec, hp, trees, None)  # the export keeps every tree it is given: exactly the trees up to the best round
    model.fit(x_train, y_train)
    metrics, confusion = classification_metrics(model.predict_proba(x_held), y_held, len(classes))
    onnx = work / "model.onnx"
    export(model, len(features), onnx)
    signature = {
        "inputs": [{"name": INPUT_NAME, "shape": ["batch", len(features)], "dtype": "float32"}],
        "outputs": [{"name": OUTPUTS[0], "shape": ["batch"], "dtype": "int64"},
                    {"name": OUTPUTS[1], "shape": ["batch", len(classes)], "dtype": "float32"}],
        "preprocessing": {"features": features, "target": target, "missing": "nan", "categories": categories}}
    return Result(classes, metrics, [{"epoch": 1, **metrics}], onnx, signature, {"labels": classes, "matrix": confusion})
