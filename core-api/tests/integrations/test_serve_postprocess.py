"""The postprocessing that runs on the Ray Serve replica, executed here from the text serve_client embeds."""
import asyncio
import base64
import re

import pytest

from app.core import task_catalog
from app.integrations import serve_client
from app.integrations.data_plane import mlflow

CLASSES = ["cat", "dog"]


def test_classification_ranks_classes_by_softmax():
    np = pytest.importorskip("numpy")  # a core-api dependency of neither the app nor its tests; the replica has it
    from app.integrations import serve_postprocess

    out = serve_postprocess.classify([np.array([[0.0, 2.0]])], CLASSES, 10, 10)
    assert [p["class"] for p in out] == ["dog", "cat"] and sum(p["probability"] for p in out) == pytest.approx(1)


def test_detection_scales_normalised_boxes_to_the_images_pixels_and_drops_low_scores():
    np = pytest.importorskip("numpy")
    from app.integrations import serve_postprocess

    logits = np.full((1, 2, 2), -20.0)
    logits[0, 1, 0] = 5.0  # query 1 is a "cat"; every other pair scores ~0
    boxes = np.array([[[0.1, 0.1, 0.1, 0.1], [0.5, 0.25, 0.2, 0.5]]])
    (found,) = serve_postprocess.detect([logits, boxes], CLASSES, 200, 100)
    assert found["class"] == "cat" and found["probability"] > serve_postprocess.DETECTION_SCORE_THRESHOLD
    assert found["box"] == pytest.approx({"x": 80.0, "y": 0.0, "width": 40.0, "height": 50.0})


def test_every_servable_task_has_a_postprocessor():
    assert set(serve_client.SERVING) == set(task_catalog.SERVABLE)


@pytest.mark.parametrize("task", task_catalog.SERVABLE)
def test_the_deployed_script_compiles_and_calls_its_tasks_postprocessor(monkeypatch, task):
    scripts = []

    async def submit(spec):
        scripts.append(spec.entrypoint)
        return "job-1"

    async def run_model_url(*_):
        return "http://m/model.onnx"

    monkeypatch.setattr(serve_client.compute, "submit", submit)
    monkeypatch.setattr(mlflow, "run_model_url", run_model_url)
    asyncio.run(serve_client.deploy(deployment_id="d", task=task, mlflow_run_id="r", class_names=CLASSES, tenant_id="t", project_id="p"))
    script = base64.b64decode(re.search(r"b64decode\('([^']+)'\)", scripts[0]).group(1)).decode()
    compile(script, "serve", "exec")
    serving = serve_client.SERVING[task]
    assert f"{serving.postprocess}(outputs, class_names, width, height)" in script
    assert f"self.encode = {serving.encoder}(" in script and f"def {serving.encoder}(" in script


def test_text_is_tokenized_to_the_trained_length_and_the_tokenizer_is_fetched_beside_the_model(monkeypatch):
    np = pytest.importorskip("numpy")
    tokenizers = pytest.importorskip("tokenizers")
    tk = tokenizers.Tokenizer(tokenizers.models.WordLevel({"[UNK]": 0, "great": 1, "film": 2}, unk_token="[UNK]"))
    tk.pre_tokenizer = tokenizers.pre_tokenizers.Whitespace()
    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout: type("R", (), {"read": lambda self: tk.to_str().encode()})())
    scope = {}
    exec(serve_client._source("serve_inputs.py"), scope)
    feeds, width, height = scope["text_encoder"]({"max_length": 4}, "http://m/tokenizer.json", "input_ids")({"text": "great film"})
    assert feeds["input_ids"].tolist() == [[1, 2, 0, 0]] and feeds["attention_mask"].tolist() == [[1, 1, 0, 0]]
    assert (width, height) == (0, 0) and feeds["input_ids"].dtype == np.int64
