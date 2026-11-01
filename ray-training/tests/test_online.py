"""Real pretrained checkpoints from the Hugging Face Hub. Off by default (no network in the default run):
TRAIN_TESTS_ONLINE=1 .venv/bin/python -m pytest tests/test_online.py. Skipped when the Hub is unreachable."""
import os
import socket

import pytest

from ray_training.constants import MODEL_KEY
from ray_training.runner import run
from ray_training.spec import parse_spec
from tests.support import MemStore, classification_rows, detection_rows, fake_get, spec_text

pytestmark = pytest.mark.online


SMALL_BASE_MODELS = {"image-classification": "facebook/convnext-tiny-224", "object-detection": "PekingU/rtdetr_v2_r18vd"}  # core-api task_catalog


def hub_reachable() -> bool:
    try:
        socket.create_connection(("huggingface.co", 443), timeout=3).close()
        return True
    except OSError:
        return False


if os.environ.get("TRAIN_TESTS_ONLINE") != "1":
    pytestmark = [pytest.mark.online, pytest.mark.skip(reason="set TRAIN_TESTS_ONLINE=1 to download checkpoints")]
elif not hub_reachable():
    pytestmark = [pytest.mark.online, pytest.mark.skip(reason="Hugging Face Hub unreachable (offline)")]


@pytest.mark.parametrize("task,rows_fn", [("image-classification", classification_rows), ("object-detection", detection_rows)])
def test_the_small_presets_real_checkpoint_trains_and_exports(task, rows_fn):
    rows, files = rows_fn()
    store = MemStore()
    spec = parse_spec(spec_text(task, SMALL_BASE_MODELS[task], rows))
    spec = type(spec)(**{**spec.__dict__, "base_revision": None, "image_size": 224 if task == "image-classification" else 320})
    report = run(spec, store, fake_get(rows, files))
    assert report["status"] == "succeeded", report["message"]
    assert len(store.puts[MODEL_KEY]) > 1_000_000
