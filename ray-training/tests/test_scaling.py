"""How a preset's gpu becomes Ray Train workers."""
import pytest

pytest.importorskip("ray.train")

from ray_training.entrypoints.train import scaling  # noqa: E402
from ray_training.errors import Unsupported  # noqa: E402
from ray_training.spec import parse_spec  # noqa: E402
from tests.support import classification_rows, spec_doc  # noqa: E402


def spec_with(gpu):
    doc = spec_doc("image-classification", "m", classification_rows()[0])
    doc["resources"]["gpu"] = gpu
    import json

    return parse_spec(json.dumps(doc))


@pytest.fixture(autouse=True)
def no_override(monkeypatch):
    monkeypatch.delenv("TRAIN_NUM_GPUS", raising=False)


@pytest.mark.parametrize("gpu, workers, gpu_each", [(0, 1, None), (1, 1, 1), (4, 4, 1), (0.5, 1, 0.5)])
def test_a_preset_gpu_is_workers_with_one_gpu_each_or_a_share_of_one(gpu, workers, gpu_each):
    config = scaling(spec_with(gpu))
    assert config.num_workers == workers and config.use_gpu == bool(gpu)
    assert config.resources_per_worker.get("GPU") == gpu_each and config.resources_per_worker["CPU"] == 1


def test_a_fractional_gpu_count_above_one_is_refused():
    with pytest.raises(Unsupported):
        scaling(spec_with(1.5))


def test_the_ci_override_wins(monkeypatch):
    monkeypatch.setenv("TRAIN_NUM_GPUS", "0")
    assert not scaling(spec_with(4)).use_gpu
