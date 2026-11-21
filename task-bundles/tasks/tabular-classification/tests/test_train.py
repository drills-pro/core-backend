"""XGBoost on a tiny synthetic data.parquet: report ok, ONNX answers like the model, bad tables are bad data."""
import io
import json

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ray_core.constants import MODEL_KEY, MODEL_MANIFEST_KEY, REPORT_KEY, FailCode
from ray_core.runner import run
from ray_core.spec import parse_spec
from xgbtab import smoke

xgboost = pytest.importorskip("xgboost")  # in the trainer image (ray-training/pyproject.toml); a local venv may lack it
ort = pytest.importorskip("onnxruntime")
pytest.importorskip("onnxmltools")

DATA_URL = "mem://data.parquet"
COPIES = 10  # copies of the smoke rows in the hashed-split test


class MemStore:
    def __init__(self):
        self.puts = {}

    def put(self, key, data):
        self.puts[key] = data


def spec_for(rows, **hyperparameters):
    doc = smoke.spec_doc({"job_id": "j", "bucket": "b", "prefix": "p/"})
    doc["payload"]["data"] = {"data_url": DATA_URL}
    doc["payload"]["hyperparameters"].update(hyperparameters)
    return parse_spec(json.dumps(doc))


def getter(rows):
    sink = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), sink)
    return {DATA_URL: sink.getvalue()}.__getitem__


def rows_from_smoke():
    return pq.read_table(io.BytesIO(smoke.table())).to_pylist()


def test_xgboost_trains_exports_and_reports():
    rows, store = rows_from_smoke(), MemStore()
    report = run(spec_for(rows), store, getter(rows))
    assert report["status"] == "succeeded", report["message"]
    assert report["classes"] == ["a", "b"] and report["metrics"]["top1"] == 1.0  # separable on x
    assert json.loads(store.puts[REPORT_KEY])["status"] == "succeeded"
    manifest = json.loads(store.puts[MODEL_MANIFEST_KEY])
    assert manifest["preprocessing"]["features"] == ["x", "y", "flag"]  # the string column is dropped
    features = np.array([[1.0, 0, 0], [12.0, 1, 1]], dtype=np.float32)
    label, probabilities = ort.InferenceSession(store.puts[MODEL_KEY]).run(None, {"features": features})
    assert label.tolist() == [0, 1] and probabilities.shape == (2, 2)


def test_bad_tables_are_bad_data():
    rows = rows_from_smoke()
    report = run(spec_for(rows, target="nope"), MemStore(), getter(rows))
    assert (report["status"], report["code"]) == ("failed", FailCode.BAD_DATA)
    one_row = rows[:1]  # one row cannot be both the training and the held-out set
    assert run(spec_for(one_row), MemStore(), getter(one_row))["code"] == FailCode.BAD_DATA
    one_class = [{**r, "species": "a"} for r in rows]
    report = run(spec_for(one_class), MemStore(), getter(one_class))
    assert report["code"] == FailCode.BAD_DATA and "at least 2 classes" in report["message"]


def test_rows_all_marked_train_still_get_a_held_out_fifth():
    rows = [{**r, "split": "train"} for r in rows_from_smoke()]
    report = run(spec_for(rows), MemStore(), getter(rows))
    assert report["status"] == "succeeded", report["message"]


def test_a_string_column_with_few_values_is_a_category_the_manifest_records():
    colours = ["red", "green", "blue"]
    rows = [{**r, "colour": colours[i % 3]} for i, r in enumerate(rows_from_smoke())]
    rows[0]["colour"] = None  # a missing value stays missing
    store = MemStore()
    report = run(spec_for(rows), store, getter(rows))
    assert report["status"] == "succeeded", report["message"]
    pre = json.loads(store.puts[MODEL_MANIFEST_KEY])["preprocessing"]
    assert pre["features"] == ["x", "y", "flag", "colour"]  # `note` is unique per row: an id, not a category
    assert pre["categories"] == {"colour": ["blue", "green", "red"]}
    assert report["features"] == pre["features"] and report["categories"] == pre["categories"]
    row = np.array([[1.0, 0, 0, pre["categories"]["colour"].index("red")]], dtype=np.float32)
    label, _ = ort.InferenceSession(store.puts[MODEL_KEY]).run(None, {"features": row})
    assert label.tolist() == [0]


def test_rows_without_a_split_are_hashed_into_one():
    # the hash split is 80/10/10 per row: 40 rows can leave a split empty, 400 cannot (the rows differ by `note`, so they hash apart)
    rows = [{**r, "split": None, "note": f"{r['note']} copy {n}"} for n in range(COPIES) for r in rows_from_smoke()]
    report = run(spec_for(rows), MemStore(), getter(rows))
    assert report["status"] == "succeeded", report["message"]
