"""The replica's model handling and the smoke check, against a tiny ONNX model. Needs onnx, onnxruntime, numpy and PIL."""
import base64
import io
import json

import pytest

pytest.importorskip("onnxruntime")
pytest.importorskip("onnx")
pytest.importorskip("PIL")

from ray_serving import model
from ray_serving.entrypoints import smoke


@pytest.fixture
def spec(tmp_path):
    smoke.tiny_model(tmp_path / "model.onnx")
    (tmp_path / "model.json").write_text(json.dumps({"preprocessing": {
        "resize": [smoke.SIZE, smoke.SIZE], "resample": "bilinear", "rescale": 1 / 255, "mean": [0.5] * 3, "std": [0.25] * 3}}))
    return model.ModelSpec(
        (tmp_path / "model.onnx").as_uri(), (tmp_path / "model.json").as_uri(), "", smoke.CLASSES, {}, ["CPUExecutionProvider"],
        smoke.ENCODER, smoke.POSTPROCESS)


def body():
    from PIL import Image

    png = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 20, 20)).save(png, "PNG")
    return {"image_base64": base64.b64encode(png.getvalue()).decode()}


def test_a_loaded_model_ranks_the_classes(spec):
    answer = model.predict(model.load(spec), body())
    assert [p["class"] for p in answer] in (["a", "b"], ["b", "a"]) and sum(p["probability"] for p in answer) == pytest.approx(1)


def test_a_missing_model_is_model_load_failed_and_an_unknown_function_is_its_own_code(spec):
    with pytest.raises(model.ServeError) as err:
        model.load(model.ModelSpec(**{**spec.__dict__, "model_url": "file:///nope.onnx"}))
    assert err.value.code == model.ErrorCode.MODEL_LOAD_FAILED
    with pytest.raises(model.ServeError) as err:
        model.load(model.ModelSpec(**{**spec.__dict__, "postprocess": "nope"}))
    assert err.value.code == model.ErrorCode.UNKNOWN_FUNCTION


def test_a_body_the_encoder_cannot_read_is_bad_input(spec):
    with pytest.raises(model.ServeError) as err:
        model.predict(model.load(spec), {"image_base64": "not an image"})
    assert err.value.code == model.ErrorCode.BAD_INPUT


def test_a_spec_with_a_missing_field_is_bad_input():
    with pytest.raises(model.ServeError) as err:
        model.ModelSpec.from_dict({"model_url": "x"})
    assert err.value.code == model.ErrorCode.BAD_INPUT


def test_the_smoke_check_passes_and_writes_its_verdict():
    puts = {}
    store = type("S", (), {"put": lambda self, key, data: puts.__setitem__(key, data)})()
    pytest.importorskip("ray.serve")
    assert smoke.main(["", json.dumps({"job_id": "j", "bucket": "b", "prefix": "p/"})], store) == 0
    assert json.loads(puts["smoke.json"]) == {"ok": True}
