"""The smoke job of this bundle: a tiny RT-DETRv2 built from a config (no base model download) on 4 generated images with boxes.

`spec_doc` is the manifest's `smoke`: ray_core.entrypoints.smoke calls it with {"job_id", "bucket", "prefix", ...} and runs the
train spec it returns through the real driver. The data travels in the spec as `data:` URLs, so any node can read it.
"""
import io
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image
from transformers import RTDetrResNetConfig, RTDetrV2Config, RTDetrV2ForObjectDetection

from objdet import train as detection
from ray_core.constants import SPEC_VERSION, Box, Col, JobKind, Resample, Split
from ray_core.data import Get, load_image_dataset
from ray_core.result import Result
from ray_core.spec import TrainSpec
from ray_core.store import data_url

IMAGE_SIZE = 32  # a multiple of the largest feature stride
SOURCE_SIZE = 40
IMAGES = 4
VALIDATION_FROM = 3
EPOCHS = 2  # more than one, so a checkpoint is written and read
BATCH_SIZE = 4  # the 3 training images in one batch: BatchNorm needs more than one value per channel
MAX_RUNTIME_S = 600
CATEGORIES = ["cat", "dog"]
TRAINER = "objdet.smoke:train"
TASK = "object-detection"
MEMORY_BYTES = 1 << 30
LEARNING_RATE = 1e-3


def tiny(num_labels: int) -> RTDetrV2ForObjectDetection:
    backbone = RTDetrResNetConfig(
        embedding_size=8, hidden_sizes=[8, 16, 32, 64], depths=[1, 1, 1, 1], out_features=["stage2", "stage3", "stage4"],
        layer_type="basic", downsample_in_bottleneck=False)
    return RTDetrV2ForObjectDetection(RTDetrV2Config(
        backbone_config=backbone, encoder_hidden_dim=16, d_model=16, encoder_in_channels=[16, 32, 64], feat_strides=[8, 16, 32],
        num_queries=10, decoder_layers=2, encoder_layers=1, encoder_ffn_dim=32, decoder_ffn_dim=32,  # >= 2 decoder layers: weight tying
        encoder_attention_heads=2, decoder_attention_heads=2, decoder_in_channels=[16, 16, 16], num_labels=num_labels,
        num_feature_levels=3, decoder_n_points=2, num_denoising=0, anchor_image_size=None))


def train(spec: TrainSpec, work: Path, get: Get) -> Result | None:
    ds = load_image_dataset(spec, work, get, detection=True)
    return detection.train_model(spec, ds, work, model=tiny(len(ds.classes)))


def png(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (SOURCE_SIZE, SOURCE_SIZE), color).save(buf, "PNG")
    return buf.getvalue()


def fixture_data() -> dict:
    """index.parquet (one box per image) and the manifest of the images it lists, as `data:` URLs."""
    box = pa.struct([(f.value, pa.string() if f == Box.CATEGORY else pa.float64()) for f in Box])
    schema = pa.schema([(c.value, {Col.SIZE: pa.int64(), Col.BOXES: pa.list_(box)}.get(c, pa.string())) for c in Col])
    rows, objects = [], {}
    for i in range(IMAGES):
        path = f"{i}.png"
        objects[path] = data_url(png((50 * i, 80, 120)))
        boxes = [{Box.CATEGORY.value: CATEGORIES[i % len(CATEGORIES)], Box.X.value: 5.0 + i, Box.Y.value: 6.0, Box.WIDTH.value: 20.0, Box.HEIGHT.value: 18.0}]
        rows.append({Col.PATH.value: path, Col.SIZE.value: 0, Col.SHA256.value: f"{i:02x}" * 32, Col.LABEL.value: None, Col.BOXES.value: boxes,
                     Col.SPLIT.value: (Split.VALIDATION if i >= VALIDATION_FROM else Split.TRAIN).value})
    sink = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), sink)
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
