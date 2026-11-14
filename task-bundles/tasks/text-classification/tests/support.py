"""Tiny fixtures: 8 labelled texts. All CPU, no network."""
import io
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from ray_core.constants import LABEL_COLUMN, TEXT_COLUMN, Col

REPO = Path(__file__).resolve().parents[4]  # core-backend, while the bundles live there; elsewhere the schema check is skipped
REPORT_SCHEMA_FILE = REPO / "core-api/app/integrations/data_plane/train_report.schema.json"
REPORT_SCHEMA = json.loads(REPORT_SCHEMA_FILE.read_text()) if REPORT_SCHEMA_FILE.exists() else {}  # {} validates anything
SIZE = 32  # image side in pixels and image_size of the tiny runs
INDEX_URL = "mem://index.parquet"
MANIFEST_URL = "mem://manifest.json"
PREPROCESSING = {"mean": [0.5, 0.5, 0.5], "std": [0.25, 0.25, 0.25], "rescale": 1 / 255, "resample": "bilinear"}  # not ImageNet: proves the spec is what is used
LEARNING_RATE = 1e-3
VERSION_ID = "6f1c5a5e-2a39-4d57-9d65-0d8f0d3b5a11"


TEXT_VOCAB = ["great", "good", "love", "awful", "bad", "hate", "movie", "film"]
TEXT_DATA_URL = "mem://data.parquet"
MAX_LENGTH = 8


def text_rows() -> list[dict]:
    """8 short texts, 2 classes; split column set: 6 train, 2 validation."""
    rows = []
    for i in range(8):
        good = i % 2 == 0
        rows.append({TEXT_COLUMN: f"{'great love' if good else 'awful hate'} {'movie' if i % 4 < 2 else 'film'}",
                     LABEL_COLUMN: "pos" if good else "neg", Col.SPLIT: "validation" if i >= 6 else "train"})
    return rows


def text_get(rows):
    sink = io.BytesIO()
    pq.write_table(pa.Table.from_pylist([{str(k.value if hasattr(k, "value") else k): v for k, v in r.items()} for r in rows]), sink)
    return lambda url: {TEXT_DATA_URL: sink.getvalue()}[url]


def text_spec_text(base_model, **payload) -> str:
    return spec_text("text-classification", base_model, [], preprocessing={"max_length": MAX_LENGTH},
                     data={"data_url": TEXT_DATA_URL, "version_id": VERSION_ID, "lakefs_commit": "c0ffee"}, **payload)


def spec_doc(task, base_model, rows, kind="train", **payload) -> dict:
    return {
        "spec_version": 1, "kind": kind, "job_id": "job-1", "tenant_id": "9d1ad1c3-6e0e-4f54-8f0b-2c7a3a1d5b10",
        "resources": {"cpu": 1, "memory_bytes": 2**30, "gpu": 1, "max_runtime_s": 0},
        "payload": {
            "bucket": "b", "prefix": "trainings/t/p/r/", "task": task, "trainer": "textcls.train:train", "data_kind": "images", "preset": "small",
            "base_model": {"repo_id": str(base_model), "revision": "0" * 40},
            "learning_rate": LEARNING_RATE, "preprocessing": PREPROCESSING,
            "data": {"index_url": INDEX_URL, "manifest_url": MANIFEST_URL, "version_id": VERSION_ID, "lakefs_commit": "c0ffee"},
            "hyperparameters": {"epochs": 1, "batch_size": 4, "image_size": SIZE},
            "seed": 42, **payload}}


def spec_text(*args, **kw) -> str:
    return json.dumps(spec_doc(*args, **kw))


class MemStore:
    def __init__(self):
        self.puts = {}

    def put(self, key, data):
        self.puts[key] = data
