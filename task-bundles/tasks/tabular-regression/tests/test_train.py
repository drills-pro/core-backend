"""XGBoost regression on a tiny synthetic data.parquet: report ok, ONNX answers like the model, bad tables are bad data."""
import io
import json

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ray_core.constants import MODEL_KEY, MODEL_MANIFEST_KEY, REPORT_KEY, FailCode
from ray_core.runner import run
from ray_core.spec import parse_spec
from xgbreg import smoke

xgboost = pytest.importorskip("xgboost")  # in the trainer image (ray-training/pyproject.toml); a local venv may lack it
ort = pytest.importorskip("onnxruntime")
pytest.importorskip("onnxmltools")

DATA_URL = "mem://data.parquet"


class MemStore:
    def __init__(self):
        self.puts = {}

    def put(self, key, data):
        self.puts[key] = data


def spec_for(**hyperparameters):
    doc = smoke.spec_doc({"job_id": "j", "bucket": "b", "prefix": "p/"})
    doc["payload"]["data"] = {"data_url": DATA_URL}
    doc["payload"]["hyperparameters"].update({"n_estimators": 60, **hyperparameters})
    return parse_spec(json.dumps(doc))


def getter(rows):
    sink = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), sink)
    return {DATA_URL: sink.getvalue()}.__getitem__


def rows_from_smoke():
    return pq.read_table(io.BytesIO(smoke.table())).to_pylist()


def test_xgboost_regression_trains_exports_and_reports():
    rows, store = rows_from_smoke(), MemStore()
    report = run(spec_for(), store, getter(rows))
    assert report["status"] == "succeeded", report["message"]
    assert report["classes"] == [] and report["confusion_matrix"] is None and set(report["metrics"]) == {"rmse", "mae", "r2"}
    assert report["metrics"]["r2"] > 0.9  # price follows x and the size
    assert json.loads(store.puts[REPORT_KEY])["status"] == "succeeded"
    pre = json.loads(store.puts[MODEL_MANIFEST_KEY])["preprocessing"]
    assert pre["features"] == ["x", "y", "size"] and pre["categories"] == {"size": ["large", "small"]}  # `note` is an id
    row = np.array([[3.0, 0, pre["categories"]["size"].index("large")]], dtype=np.float32)
    (value,) = ort.InferenceSession(store.puts[MODEL_KEY]).run(None, {"features": row})
    assert value.shape == (1, 1) and abs(float(value[0, 0]) - 130.0) < 15


def test_bad_tables_are_bad_data():
    rows = rows_from_smoke()
    words = [{**r, "price": "dear"} for r in rows]
    report = run(spec_for(), MemStore(), getter(words))
    assert (report["status"], report["code"]) == ("failed", FailCode.BAD_DATA) and "numbers" in report["message"]
    assert run(spec_for(target="nope"), MemStore(), getter(rows))["code"] == FailCode.BAD_DATA
    assert run(spec_for(), MemStore(), getter(rows[:1]))["code"] == FailCode.BAD_DATA  # one row cannot be both sides
    only_ids = [{k: v for k, v in r.items() if k in ("note", "price", "split")} for r in rows]
    assert run(spec_for(), MemStore(), getter(only_ids))["code"] == FailCode.BAD_DATA  # nothing to learn from


def test_a_constant_target_scores_r2_zero_not_nan():
    rows = [{**r, "price": 5.0} for r in rows_from_smoke()]
    report = run(spec_for(), MemStore(), getter(rows))
    assert report["status"] == "succeeded", report["message"]
    assert report["metrics"]["r2"] == 0.0


def test_rows_without_a_split_are_hashed_and_all_train_rows_get_a_held_out_fifth():
    rows = [{**r, "split": None, "note": f"{r['note']} copy {n}"} for n in range(10) for r in rows_from_smoke()]
    assert run(spec_for(), MemStore(), getter(rows))["status"] == "succeeded"
    all_train = [{**r, "split": "train"} for r in rows_from_smoke()]
    assert run(spec_for(), MemStore(), getter(all_train))["status"] == "succeeded"
