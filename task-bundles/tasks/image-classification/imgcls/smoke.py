"""The smoke job of this bundle: classification of a tiny linear model on 8 generated images, so it needs no base model download.

`spec_doc` is the manifest's `smoke`: ray_core.entrypoints.smoke calls it with {"job_id", "bucket", "prefix", ...} and runs the
train spec it returns through the real driver. The data travels in the spec as `data:` URLs, so any node can read it.
"""
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import torch
from PIL import Image

from imgcls import train as classification
from ray_core.constants import SPEC_VERSION, Col, JobKind, Resample, Split
from ray_core.data import Get, load_image_dataset
from ray_core.result import Result
from ray_core.spec import TrainSpec
from ray_core.store import data_url

CHANNELS = 3
IMAGE_SIZE = 8
IMAGES = 8
VALIDATION_FROM = 6  # the last two images are the validation split
EPOCHS = 2  # more than one, so a checkpoint is written and read
BATCH_SIZE = 4
MAX_RUNTIME_S = 600
CLASS_COLORS = {"red": (200, 20, 20), "blue": (20, 20, 200)}
TRAINER = "imgcls.smoke:train"
TASK = "image-classification"
MEMORY_BYTES = 1 << 30
LEARNING_RATE = 1e-2


class Tiny(torch.nn.Module):
    """Answers like a Hugging Face image classifier: `.config.num_labels`, forward(pixel_values, labels) -> .logits, .loss."""

    def __init__(self, image_size: int, num_labels: int):
        super().__init__()
        self.config = SimpleNamespace(num_labels=num_labels)
        self.fc = torch.nn.Linear(CHANNELS * image_size * image_size, num_labels)

    def forward(self, pixel_values, labels=None):
        logits = self.fc(pixel_values.flatten(1))
        return SimpleNamespace(logits=logits, loss=None if labels is None else torch.nn.functional.cross_entropy(logits, labels))


def train(spec: TrainSpec, work: Path, get: Get) -> Result | None:
    ds = load_image_dataset(spec, work, get, detection=False)
    return classification.train_model(spec, ds, work, model=Tiny(spec.image_size, len(ds.classes)))


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
        rows.append({Col.PATH.value: path, Col.SIZE.value: 0, Col.SHA256.value: f"{i:02x}" * 32, Col.LABEL.value: label, Col.BOXES.value: None,
                     Col.SPLIT.value: (Split.VALIDATION if i >= VALIDATION_FROM else Split.TRAIN).value})
    sink = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), sink)
    return {"index_url": data_url(sink.getvalue()), "manifest_url": data_url(json.dumps(objects).encode())}


def spec_doc(arg: dict) -> dict:
    return {
        "spec_version": SPEC_VERSION, "kind": JobKind.TRAIN, "job_id": arg["job_id"], "tenant_id": "smoke",
        "resources": {"cpu": 1, "memory_bytes": MEMORY_BYTES, "gpu": 0, "max_runtime_s": MAX_RUNTIME_S},
        "payload": {
            "bucket": arg["bucket"], "prefix": arg["prefix"], "task": TASK, "trainer": TRAINER, "preset": "smoke",
            "base_model": {"repo_id": "smoke"}, "learning_rate": LEARNING_RATE,
            "preprocessing": {"mean": [0.5] * 3, "std": [0.25] * 3, "rescale": 1 / 255, "resample": Resample.BILINEAR},
            "data": fixture_data(),
            "hyperparameters": {"epochs": EPOCHS, "batch_size": BATCH_SIZE, "image_size": IMAGE_SIZE}}}
