"""The smoke job of this bundle: a few XGBoost trees on 40 generated rows (two numeric features, one category, one id to drop).

`spec_doc` is the manifest's `smoke`: ray_core.entrypoints.smoke calls it with {"job_id", "bucket", "prefix", ...} and runs the
train spec it returns through the real driver. The data travels in the spec as a `data:` URL, so any node can read it.
"""
import io

import pyarrow as pa
import pyarrow.parquet as pq

from ray_core.constants import SPEC_VERSION, Col, JobKind, Split
from ray_core.store import data_url

ROWS = 40
VALIDATION_EVERY = 5  # every fifth row is the validation split
TARGET = "price"
MAX_RUNTIME_S = 600
TRAINER = "xgbreg.train:train"
TASK = "tabular-regression"
MEMORY_BYTES = 1 << 30
LEARNING_RATE = 0.3
HYPERPARAMETERS = {"target": TARGET, "n_estimators": 5, "max_depth": 2}
SIZES = ("small", "large")


def table() -> bytes:
    """price = 10 * x + 100 when the size is large: x explains it, the size shifts it."""
    rows = []
    for i in range(ROWS):
        x, size = float(i % 7), SIZES[i % 2]
        rows.append({"x": x, "y": i % 3, "size": size, "note": f"row {i}", TARGET: 10 * x + (100.0 if size == "large" else 0.0),
                     Col.SPLIT.value: (Split.VALIDATION if i % VALIDATION_EVERY == 0 else Split.TRAIN).value})
    sink = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), sink)
    return sink.getvalue()


def spec_doc(arg: dict) -> dict:
    return {
        "spec_version": SPEC_VERSION, "kind": JobKind.TRAIN, "job_id": arg["job_id"], "tenant_id": "smoke",
        "resources": {"cpu": 1, "memory_bytes": MEMORY_BYTES, "gpu": 0, "max_runtime_s": MAX_RUNTIME_S},
        "payload": {
            "bucket": arg["bucket"], "prefix": arg["prefix"], "task": TASK, "trainer": TRAINER, "preset": "smoke",
            "base_model": {"repo_id": "xgboost"}, "learning_rate": LEARNING_RATE, "preprocessing": {},
            "data": {"data_url": data_url(table())}, "hyperparameters": dict(HYPERPARAMETERS)}}  # a copy: callers may edit the spec
