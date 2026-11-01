"""One epoch on the tiny fixtures, on CPU: report ok, metrics present, ONNX loads in onnxruntime."""
import json

import jsonschema
import numpy as np
import onnxruntime as ort
import pytest

from ray_training.constants import MODEL_KEY, MODEL_MANIFEST_KEY, REPORT_KEY
from ray_training.runner import run
from ray_training.spec import parse_spec
from tests.support import PREPROCESSING, REPORT_SCHEMA, SIZE, MemStore, classification_rows, detection_rows, fake_get, spec_text


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


def test_detection_trains_exports_and_reports(tiny_rtdetr):
    rows, files = detection_rows()
    store = MemStore()
    report = run(parse_spec(spec_text("object-detection", tiny_rtdetr, rows)), store, fake_get(rows, files))
    assert report["status"] == "succeeded", report["message"]
    jsonschema.validate(report, REPORT_SCHEMA)
    assert "artifacts" not in report
    assert set(report["metrics"]) == {"map50", "map50_95"} and report["confusion_matrix"] is None
    logits, boxes = session(store, 3)
    assert logits.shape == (3, 10, 2) and boxes.shape == (3, 10, 4)
    assert [o["name"] for o in json.loads(store.puts[MODEL_MANIFEST_KEY])["outputs"]] == ["logits", "pred_boxes"]


def test_the_base_model_must_exist_so_a_bad_one_is_a_failed_report_not_a_crash(tmp_path):
    rows, files = classification_rows()
    store = MemStore()
    report = run(parse_spec(spec_text("image-classification", tmp_path / "nope", rows)), store, fake_get(rows, files))
    assert report["status"] == "failed" and report["code"] == "internal" and REPORT_KEY in store.puts


def test_load_image_applies_the_specs_normalisation(tmp_path):
    from ray_training.result import load_image
    from ray_training.spec import parse_spec
    from tests.support import png

    file = tmp_path / "gray.png"
    file.write_bytes(png((51, 51, 51)))  # 51 / 255 = 0.2
    pre = parse_spec(spec_text("image-classification", "m", classification_rows()[0])).preprocessing
    tensor, w, h = load_image(file, 8, pre)
    assert (w, h, tuple(tensor.shape)) == (SIZE, SIZE, (3, 8, 8))
    assert np.allclose(tensor.numpy(), (0.2 - 0.5) / 0.25, atol=1e-5)  # PREPROCESSING: mean 0.5, std 0.25
