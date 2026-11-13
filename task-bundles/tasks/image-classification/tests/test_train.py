"""One epoch on the tiny fixtures, on CPU: report ok, metrics present, ONNX loads in onnxruntime."""
import json
import re
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import numpy as np
import onnxruntime as ort
import torch

from imgcls.train import Images
from ray_core.constants import MODEL_KEY, MODEL_MANIFEST_KEY, REPORT_KEY, Augmentation, Resample
from ray_core.runner import run
from ray_core.spec import Preprocessing, parse_spec
from tests.support import PREPROCESSING, REPORT_SCHEMA, SIZE, MemStore, classification_rows, fake_get, spec_text

SRC = Path(__file__).parents[1] / "imgcls"


def session(store: MemStore, batch: int) -> list[np.ndarray]:
    onnx = ort.InferenceSession(store.puts[MODEL_KEY])
    return onnx.run(None, {"pixel_values": np.zeros((batch, 3, SIZE, SIZE), dtype=np.float32)})


def test_classification_trains_exports_and_reports(tiny_convnext):
    rows, files = classification_rows()
    store = MemStore()
    report = run(parse_spec(spec_text("image-classification", tiny_convnext, rows)), store, fake_get(rows, files))
    assert report["status"] == "succeeded", report["message"]
    jsonschema.validate(report, REPORT_SCHEMA)
    assert set(report["metrics"]) == {"top1", "top5"} and len(report["history"]) == 1
    assert report["confusion_matrix"]["labels"] == ["blue", "red"] and np.array(report["confusion_matrix"]["matrix"]).sum() == 2
    assert json.loads(store.puts[REPORT_KEY]) == json.loads(json.dumps(report, default=str))
    (logits,) = session(store, 3)  # dynamic batch axis
    assert logits.shape == (3, 2)
    manifest = json.loads(store.puts[MODEL_MANIFEST_KEY])
    assert manifest["classes"] == ["blue", "red"] and manifest["lakefs_commit"] == "c0ffee" and manifest["format"] == "onnx"
    assert manifest["preprocessing"] == {
        "resize": [SIZE, SIZE], **PREPROCESSING, "layout": "NCHW", "color": "RGB"}  # exactly what the spec said, nothing ImageNet
    assert manifest["metrics"] == report["metrics"]


def test_the_base_model_must_exist_so_a_bad_one_is_a_failed_report_not_a_crash(tmp_path):
    rows, files = classification_rows()
    store = MemStore()
    report = run(parse_spec(spec_text("image-classification", tmp_path / "nope", rows)), store, fake_get(rows, files))
    assert report["status"] == "failed" and report["code"] == "internal" and REPORT_KEY in store.puts


def test_every_from_pretrained_of_a_model_refuses_pickled_weights():
    calls = re.findall(r"Model\w*\.from_pretrained\((.*?)\)\n", (SRC / "train.py").read_text(), re.S)
    assert calls and all("use_safetensors=True" in c for c in calls)


def test_only_the_train_split_is_augmented(monkeypatch):
    pre = Preprocessing((0.0,) * 3, (1.0,) * 3, 1.0, Resample.BILINEAR)
    calls = []
    monkeypatch.setattr("imgcls.train.flip_crop", lambda im: calls.append(1) or im)
    monkeypatch.setattr("imgcls.train.load_image", lambda *a: (torch.zeros(3, 4, 4), 4, 4))
    fake = [SimpleNamespace(file="x", label=1)]
    Images(fake, 4, pre)[0]
    Images(fake, 4, pre, Augmentation.NONE)[0]
    assert calls == []
    Images(fake, 4, pre, Augmentation.FLIP_CROP)[0]
    assert calls == [1]
