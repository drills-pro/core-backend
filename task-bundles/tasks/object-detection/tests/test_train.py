"""One epoch on the tiny fixtures, on CPU: report ok, mAP present, ONNX loads in onnxruntime; the recipe and the metric."""
import json
import re
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import numpy as np
import onnxruntime as ort
import pytest
import torch

from objdet.metrics import detection_metrics
from objdet.train import Images
from ray_core.constants import MODEL_KEY, MODEL_MANIFEST_KEY, Augmentation, Resample
from ray_core.runner import run
from ray_core.spec import Preprocessing, parse_spec
from tests.support import REPORT_SCHEMA, SIZE, MemStore, detection_rows, fake_get, spec_text

SRC = Path(__file__).parents[1] / "objdet"


def test_detection_trains_exports_and_reports(tiny_rtdetr):
    rows, files = detection_rows()
    store = MemStore()
    report = run(parse_spec(spec_text("object-detection", tiny_rtdetr, rows)), store, fake_get(rows, files))
    assert report["status"] == "succeeded", report["message"]
    jsonschema.validate(report, REPORT_SCHEMA)
    assert "artifacts" not in report
    assert set(report["metrics"]) == {"map50", "map50_95"} and report["confusion_matrix"] is None
    assert report["classes"] and report["classes"] == json.loads(store.puts[MODEL_MANIFEST_KEY])["classes"]  # serving names detections with these
    logits, boxes = ort.InferenceSession(store.puts[MODEL_KEY]).run(None, {"pixel_values": np.zeros((3, 3, SIZE, SIZE), dtype=np.float32)})
    assert logits.shape == (3, 10, 2) and boxes.shape == (3, 10, 4)
    assert [o["name"] for o in json.loads(store.puts[MODEL_MANIFEST_KEY])["outputs"]] == ["logits", "pred_boxes"]


def test_every_from_pretrained_of_a_model_refuses_pickled_weights():
    calls = re.findall(r"ForObjectDetection\.from_pretrained\((.*?)\)\n", (SRC / "train.py").read_text(), re.S)
    assert calls and all("use_safetensors=True" in c for c in calls)


def test_detection_flip_moves_the_boxes_with_the_image(monkeypatch):
    pre = Preprocessing((0.0,) * 3, (1.0,) * 3, 1.0, Resample.BILINEAR)
    monkeypatch.setattr("objdet.train.load_image", lambda *a: (torch.zeros(3, 4, 4), 100, 100))
    monkeypatch.setattr("ray_core.recipe.random.random", lambda: 0.0)  # always flip
    sample = SimpleNamespace(file="x", boxes=[(0, 10, 20, 20, 40)])  # x, y, w, h in pixels: cx = 0.2
    _, plain = Images([sample], 4, pre)[0]
    _, flipped = Images([sample], 4, pre, Augmentation.FLIP)[0]
    assert plain["boxes"][0, 0].item() == pytest.approx(0.2) and flipped["boxes"][0, 0].item() == pytest.approx(0.8)
    assert flipped["boxes"][0, 1:].tolist() == pytest.approx(plain["boxes"][0, 1:].tolist())


def test_map_perfect_wrong_and_shifted():
    gt = [[(0, [0.1, 0.1, 0.5, 0.5])], [(1, [0.2, 0.2, 0.6, 0.6])]]
    perfect = [[(c, 0.9, b)] for [(c, b)] in gt]
    assert detection_metrics(perfect, gt, 2) == {"map50": 1.0, "map50_95": 1.0}
    assert detection_metrics([[], []], gt, 2) == {"map50": 0.0, "map50_95": 0.0}
    wrong_class = [[(1 - c, 0.9, b)] for [(c, b)] in gt]
    assert detection_metrics(wrong_class, gt, 2)["map50"] == 0.0
    shifted = [[(c, 0.9, [b[0] + 0.05, b[1], b[2] + 0.05, b[3]])] for [(c, b)] in gt]  # IoU ~0.78
    m = detection_metrics(shifted, gt, 2)
    assert m["map50"] == 1.0 and 0.0 < m["map50_95"] < 1.0
