"""Train/serve parity: the same input goes through the training code (ray_core) and the serving code (ray_serving.inputs), and must come
out as the same numbers. The two are separate copies (separate zips), so this is the only thing that keeps them equal. It lives here, not
in core-api/tests, because it needs numpy, Pillow and torch, which ray-serving's suite runs with (ray-training's environment)."""
import base64
import json
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from ray_core.constants import Outcome, Resample
from ray_core.result import load_image, preprocessing
from ray_core.spec import Preprocessing
from ray_core.tabular import features_of, matrix
from ray_serving.entrypoints import predict_batch
from ray_serving.entrypoints.predict_batch import build_report, failed_report
from ray_serving.inputs import image_encoder, tabular_encoder

INPUT = "pixel_values"
SIZE = 24  # the trained image side; the golden images are not square and not this size, so the resize does work
PRE = {
    "imagenet": Preprocessing((0.485, 0.456, 0.406), (0.229, 0.224, 0.225), 1 / 255, Resample.BICUBIC),
    "half": Preprocessing((0.5, 0.5, 0.5), (0.25, 0.25, 0.25), 1 / 255, Resample.BILINEAR),
}


def golden_image(mode: str) -> Image.Image:
    rng = np.random.default_rng(7)
    rgb = Image.fromarray(rng.integers(0, 256, size=(37, 53, 3), dtype=np.uint8), "RGB")
    return rgb.convert(mode)


def model_json_entry(pre: Preprocessing) -> dict:
    """What the trainer writes to model.json (result.preprocessing), as serving reads it back from the file."""
    return json.loads(json.dumps(preprocessing(SimpleNamespace(preprocessing=pre, image_size=SIZE))))


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "L", "P"])
@pytest.mark.parametrize("name", PRE)
def test_an_image_becomes_the_same_tensor_at_training_and_at_serving(name, mode, tmp_path):
    image = golden_image(mode)
    path = tmp_path / "golden.png"
    image.save(path, "PNG")
    trained, width, height = load_image(path, SIZE, PRE[name])
    feeds, served_width, served_height = image_encoder(model_json_entry(PRE[name]), "", INPUT)(
        {"image_base64": base64.b64encode(path.read_bytes()).decode()})
    assert (served_width, served_height) == (width, height) == image.size
    assert feeds[INPUT].dtype == np.float32 and feeds[INPUT].shape == (1, 3, SIZE, SIZE)
    np.testing.assert_array_equal(feeds[INPUT][0], trained.numpy())


def test_every_resample_the_trainer_can_record_is_one_serving_resolves():
    for resample in Resample:
        entry = model_json_entry(Preprocessing((0, 0, 0), (1, 1, 1), 1.0, resample))
        assert getattr(Image, entry["resample"].upper()) == Image.Resampling[resample.name]


def test_the_model_json_entry_carries_everything_serving_reads():
    assert {"resize", "resample", "rescale", "mean", "std"} <= set(model_json_entry(PRE["half"]))


_ROWS = [
    {"age": 40, "income": 5.5, "member": True, "colour": "red", "id": "a", "label": 1},
    {"age": None, "income": 1.25, "member": False, "colour": "blue", "id": "b", "label": 0},
    {"age": 3, "income": None, "member": None, "colour": None, "id": "c", "label": 1},
    {"age": 0, "income": -2.0, "member": True, "colour": "green", "id": "d", "label": 0},
]
GOLDEN_ROWS = [{**r, "id": f"{r['id']}{i}"} for i in range(2) for r in _ROWS]  # twice, so a colour is few enough to be a category and an id is not


def test_tabular_rows_become_the_same_array_at_training_and_at_serving():
    features, categories = features_of(GOLDEN_ROWS, "label")
    assert features == ["age", "income", "member", "colour"] and categories == {"colour": ["blue", "green", "red"]}  # `id` is dropped
    trained = matrix(GOLDEN_ROWS, features, categories)
    pre = json.loads(json.dumps({"features": features, "target": "label", "missing": "nan", "categories": categories}))
    encode = tabular_encoder(pre, "", "features")
    for row, expected in zip(GOLDEN_ROWS, trained):
        feeds, _, _ = encode({k: v for k, v in row.items() if v is not None})  # an omitted key and a null are both missing
        np.testing.assert_array_equal(feeds["features"][0], expected)
        feeds, _, _ = encode(row)
        np.testing.assert_array_equal(feeds["features"][0], expected)
        assert feeds["features"].dtype == trained.dtype == np.float32


def test_the_batch_report_uses_the_trainers_status_words():
    """core-api reads one `status` vocabulary from both reports (TrainOutcome); the batch runner writes it as bare strings."""
    assert (predict_batch.SUCCEEDED, predict_batch.FAILED) == (Outcome.SUCCEEDED, Outcome.FAILED)
    assert build_report({}, [])["status"] == Outcome.SUCCEEDED
    assert failed_report(None, ValueError("x"))["status"] == Outcome.FAILED
