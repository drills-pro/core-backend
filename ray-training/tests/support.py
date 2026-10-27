"""Tiny fixtures: 8 classification images, 4 COCO-style detection images, tiny random models. All CPU, no network."""
import io
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image

from ray_training.constants import Box, Col

REPO = Path(__file__).resolve().parents[2]
REPORT_SCHEMA_FILE = REPO / "core-api/app/integrations/data_plane/train_report.schema.json"
REPORT_SCHEMA = json.loads(REPORT_SCHEMA_FILE.read_text()) if REPORT_SCHEMA_FILE.exists() else {}  # {} validates anything
SIZE = 32  # image side in pixels and image_size of the tiny runs
INDEX_URL = "mem://index.parquet"
MANIFEST_URL = "mem://manifest.json"
VERSION_ID = "6f1c5a5e-2a39-4d57-9d65-0d8f0d3b5a11"
BOX_TYPE = pa.struct([(f.value, pa.string() if f == Box.CATEGORY else pa.float64()) for f in Box])
SCHEMA = pa.schema([(c.value, {Col.SIZE: pa.int64(), Col.BOXES: pa.list_(BOX_TYPE)}.get(c, pa.string())) for c in Col])


def png(color, size=SIZE) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buf, "PNG")
    return buf.getvalue()


def sha(i: int) -> str:
    return f"{i:02x}" * 32


def classification_rows() -> tuple[list[dict], dict[str, bytes]]:
    """8 images, 2 classes; split column set: 6 train, 2 validation."""
    rows, files = [], {}
    for i in range(8):
        label, color = ("red", (200, 20 + i, 20)) if i % 2 == 0 else ("blue", (20, 20 + i, 200))
        path = f"{label}/{i}.png"
        files[path] = png(color)
        rows.append({Col.PATH: path, Col.SIZE: len(files[path]), Col.SHA256: sha(i), Col.LABEL: label, Col.BOXES: None,
                     Col.SPLIT: "validation" if i >= 6 else "train"})
    return rows, files


def detection_rows() -> tuple[list[dict], dict[str, bytes]]:
    """4 images (40x40), one or two boxes each, 2 categories; split: 3 train, 1 validation."""
    rows, files = [], {}
    for i in range(4):
        path = f"{i}.png"
        files[path] = png((50 * i, 80, 120), 40)
        boxes = [{Box.CATEGORY: "cat" if i % 2 else "dog", Box.X: 5.0 + i, Box.Y: 6.0, Box.WIDTH: 20.0, Box.HEIGHT: 18.0}]
        if i == 0:
            boxes.append({Box.CATEGORY: "cat", Box.X: 20.0, Box.Y: 20.0, Box.WIDTH: 10.0, Box.HEIGHT: 10.0})
        rows.append({Col.PATH: path, Col.SIZE: len(files[path]), Col.SHA256: sha(i), Col.LABEL: None, Col.BOXES: boxes,
                     Col.SPLIT: "validation" if i == 3 else "train"})
    return rows, files


def parquet(rows: list[dict]) -> bytes:
    sink = io.BytesIO()
    pq.write_table(pa.Table.from_pylist([{k.value: v for k, v in r.items()} for r in rows], schema=SCHEMA), sink)
    return sink.getvalue()


def manifest(rows) -> dict[str, str]:
    return {r[Col.PATH]: f"mem://{r[Col.PATH]}" for r in rows}


def fake_get(rows, files, manifest_rows=None):
    """get(url) for mem:// URLs, as http_get would serve the presigned ones. manifest_rows: the rows the manifest lists."""
    served = {INDEX_URL: parquet(rows), MANIFEST_URL: json.dumps(manifest(rows if manifest_rows is None else manifest_rows)).encode(),
              **{f"mem://{p}": d for p, d in files.items()}}
    return lambda url: served[url]


def spec_doc(task, base_model, rows, kind="train", **payload) -> dict:
    return {
        "spec_version": 1, "kind": kind, "job_id": "job-1", "tenant_id": "9d1ad1c3-6e0e-4f54-8f0b-2c7a3a1d5b10",
        "resources": {"cpu": 1, "memory_bytes": 2**30, "gpu": 1, "max_runtime_s": 0},
        "payload": {
            "bucket": "b", "prefix": "trainings/t/p/r/", "task": task, "preset": "small",
            "base_model": {"repo_id": str(base_model), "revision": "0" * 40},
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
