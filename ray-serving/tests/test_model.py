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


BASE = ""


@pytest.fixture
def spec(tmp_path):
    global BASE
    BASE = tmp_path.as_uri()
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
    answer = model.predict(model.load(spec, BASE), body())
    assert [p["class"] for p in answer] in (["a", "b"], ["b", "a"]) and sum(p["probability"] for p in answer) == pytest.approx(1)


def test_a_missing_model_is_model_load_failed_and_an_unknown_function_is_its_own_code(spec):
    with pytest.raises(model.ServeError) as err:
        model.load(model.ModelSpec(**{**spec.__dict__, "model_url": spec.model_url + ".nope"}), BASE)
    assert err.value.code == model.ErrorCode.MODEL_LOAD_FAILED
    with pytest.raises(model.ServeError) as err:
        model.load(model.ModelSpec(**{**spec.__dict__, "postprocess": "nope"}), BASE)
    assert err.value.code == model.ErrorCode.UNKNOWN_FUNCTION


def test_a_body_the_encoder_cannot_read_is_bad_input(spec):
    with pytest.raises(model.ServeError) as err:
        model.predict(model.load(spec, BASE), {"image_base64": "not an image"})
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


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:8265/api/jobs/", "http://kubernetes.default.svc/api",
    "http://mlflow.internal@evil.local/api/2.0/mlflow-artifacts/a", "http://mlflow.internal:5000/api/2.0/mlflow-artifacts/../../admin",
    "https://mlflow.internal:5000/api/2.0/mlflow-artifacts/a", "file:///etc/passwd"])
def test_a_url_outside_the_artifact_store_is_refused(url):
    with pytest.raises(model.ServeError) as err:
        model.check_url(url, "http://mlflow.internal:5000/api/2.0/mlflow-artifacts")
    assert err.value.code == model.ErrorCode.MODEL_LOAD_FAILED


def test_a_url_under_the_artifact_store_is_allowed():
    model.check_url("http://mlflow.internal:5000/api/2.0/mlflow-artifacts/artifacts/1/model.onnx", "http://mlflow.internal:5000")


def test_a_redirect_is_not_followed():
    import http.server
    import threading
    import urllib.error

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/")
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            model._fetch(f"http://127.0.0.1:{server.server_port}/x")
        assert err.value.code == 302
    finally:
        server.shutdown()


def test_an_image_over_the_pixel_limit_is_bad_input_before_it_is_decoded(spec, monkeypatch):
    from ray_serving import inputs

    monkeypatch.setattr(inputs, "MAX_IMAGE_PIXELS", 63)  # the 8x8 test image has 64
    with pytest.raises(model.ServeError) as err:
        model.predict(model.load(spec, BASE), body())
    assert err.value.code == model.ErrorCode.BAD_INPUT and "pixels" in str(err.value)


@pytest.mark.parametrize("wrap", [
    lambda b64: "data:image/png;base64," + b64,  # what a browser's FileReader gives
    lambda b64: b64.replace("+", "-").replace("/", "_").rstrip("="),  # URL-safe, unpadded
    lambda b64: "\n".join(b64[i:i + 20] for i in range(0, len(b64), 20)),  # wrapped lines
])
def test_an_image_may_arrive_as_a_data_uri_url_safe_or_wrapped_base64(spec, wrap):
    plain = body()
    loaded = model.load(spec, BASE)
    assert model.predict(loaded, {"image_base64": wrap(plain["image_base64"])}) == model.predict(loaded, plain)


def test_an_image_that_is_not_text_is_bad_input(spec):
    with pytest.raises(model.ServeError) as err:
        model.predict(model.load(spec, BASE), {"image_base64": 12})
    assert err.value.code == model.ErrorCode.BAD_INPUT
