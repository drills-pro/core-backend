"""Smoke job: a Ray Train run of a tiny model on 8 generated images, to check a package before it is promoted.

    python -m ray_training.entrypoints.smoke '{"job_id": ..., "bucket": ..., "prefix": ...}'      # STAGING_* in the environment

The data travels in the spec as `data:` URLs, so any node can read it. Writes smoke.json ({"ok": bool}) under the prefix;
exit code 1 when the run failed.
"""
import base64
import io
import json
import sys

import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image

from ray_training.constants import LABEL_COLUMN, SMOKE_KEY, SPEC_VERSION, Col, JobKind, Resample, Split, Task
from ray_training.entrypoints import train
from ray_training.store import store_from_env

IMAGE_SIZE = 8
IMAGES = 8
VALIDATION_FROM = 6  # the last two images are the validation split
EPOCHS = 2  # more than one, so a checkpoint is written and read
BATCH_SIZE = 4
MAX_RUNTIME_S = 600
CLASS_COLORS = {"red": (200, 20, 20), "blue": (20, 20, 200)}
TRAINER = "ray_training.smoke:train"
MEMORY_BYTES = 1 << 30
LEARNING_RATE = 1e-2


def data_url(data: bytes) -> str:
    return "data:application/octet-stream;base64," + base64.b64encode(data).decode()


def png(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (IMAGE_SIZE, IMAGE_SIZE), color).save(buf, "PNG")
    return buf.getvalue()


def fixture_data() -> dict:
    """index.parquet and the manifest of the images it lists, as `data:` URLs."""
    rows, objects = [], {}
    for i in range(IMAGES):
        label = list(CLASS_COLORS)[i % len(CLASS_COLORS)]
        path = f"{label}/{i}.png"
        objects[path] = data_url(png(CLASS_COLORS[label]))
        rows.append({Col.PATH.value: path, Col.SIZE.value: 0, Col.SHA256.value: f"{i:02x}" * 32, LABEL_COLUMN: label, Col.BOXES.value: None,
                     Col.SPLIT.value: (Split.VALIDATION if i >= VALIDATION_FROM else Split.TRAIN).value})
    sink = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), sink)
    return {"index_url": data_url(sink.getvalue()), "manifest_url": data_url(json.dumps(objects).encode())}


def spec_doc(arg: dict) -> dict:
    return {
        "spec_version": SPEC_VERSION, "kind": JobKind.TRAIN, "job_id": arg["job_id"], "tenant_id": "smoke",
        "resources": {"cpu": 1, "memory_bytes": MEMORY_BYTES, "gpu": 0, "max_runtime_s": MAX_RUNTIME_S},
        "payload": {
            "bucket": arg["bucket"], "prefix": arg["prefix"], "task": Task.IMAGE_CLASSIFICATION, "trainer": TRAINER, "preset": "smoke",
            "base_model": {"repo_id": "smoke"}, "learning_rate": LEARNING_RATE,
            "preprocessing": {"mean": [0.5] * 3, "std": [0.25] * 3, "rescale": 1 / 255, "resample": Resample.BILINEAR},
            "data": fixture_data(),
            "hyperparameters": {"epochs": EPOCHS, "batch_size": BATCH_SIZE, "image_size": IMAGE_SIZE}}}


def main(argv: list[str]) -> int:
    arg = json.loads(argv[1])
    code = train.main([argv[0], json.dumps(spec_doc(arg))])
    store_from_env(arg["bucket"], arg["prefix"]).put(SMOKE_KEY, json.dumps({"ok": code == 0}).encode())
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
