"""The postprocessing that runs on the Ray Serve replica, executed here from the ray_serving source."""
import types

import pytest

from app.core.task_types import ServingParams
from app.integrations import serve_client
from tests.support.task_types import SERVABLE, SERVING

CLASSES = ["cat", "dog"]
PARAMS = ServingParams().model_dump(mode="json")


def _postprocess():
    """The ray_serving postprocess file, run as the module it is on a replica."""
    scope = {}
    exec(serve_client._source(serve_client.POSTPROCESS_FILE), scope)
    return types.SimpleNamespace(**scope)


def test_classification_ranks_classes_by_softmax():
    np = pytest.importorskip("numpy")  # a core-api dependency of neither the app nor its tests; the replica has it
    serve_postprocess = _postprocess()

    out = serve_postprocess.classify([np.array([[0.0, 2.0]])], CLASSES, 10, 10, PARAMS)
    assert [p["class"] for p in out] == ["dog", "cat"] and sum(p["probability"] for p in out) == pytest.approx(1)


def test_detection_scales_normalised_boxes_to_the_images_pixels_and_drops_low_scores():
    np = pytest.importorskip("numpy")
    serve_postprocess = _postprocess()

    logits = np.full((1, 2, 2), -20.0)
    logits[0, 1, 0] = 5.0  # query 1 is a "cat"; every other pair scores ~0
    boxes = np.array([[[0.1, 0.1, 0.1, 0.1], [0.5, 0.25, 0.2, 0.5]]])
    (found,) = serve_postprocess.detect([logits, boxes], CLASSES, 200, 100, PARAMS)
    assert found["class"] == "cat" and found["probability"] > PARAMS["score_threshold"]
    assert found["box"] == pytest.approx({"x": 80.0, "y": 0.0, "width": 40.0, "height": 50.0})


def test_every_servable_task_names_functions_the_serve_files_define():
    assert set(SERVING) == set(SERVABLE)
    for encoder, postprocess in SERVING.values():
        assert serve_client.unknown_serving_functions(encoder, postprocess) == []
    assert serve_client.unknown_serving_functions("nope", None) == ["nope"]


def test_text_is_tokenized_to_the_trained_length_and_the_tokenizer_is_fetched_beside_the_model(monkeypatch):
    np = pytest.importorskip("numpy")
    tokenizers = pytest.importorskip("tokenizers")
    tk = tokenizers.Tokenizer(tokenizers.models.WordLevel({"[UNK]": 0, "great": 1, "film": 2}, unk_token="[UNK]"))
    tk.pre_tokenizer = tokenizers.pre_tokenizers.Whitespace()
    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout: type("R", (), {"read": lambda self: tk.to_str().encode()})())
    scope = {}
    exec(serve_client._source(serve_client.INPUTS_FILE), scope)
    feeds, width, height = scope["text_encoder"]({"max_length": 4}, "http://m/tokenizer.json", "input_ids")({"text": "great film"})
    assert feeds["input_ids"].tolist() == [[1, 2, 0, 0]] and feeds["attention_mask"].tolist() == [[1, 1, 0, 0]]
    assert (width, height) == (0, 0) and feeds["input_ids"].dtype == np.int64
