"""Real pretrained checkpoints from the Hugging Face Hub. Off by default (no network in the default run):
TRAIN_TESTS_ONLINE=1 .venv/bin/python -m pytest tests/test_online.py. Skipped when the Hub is unreachable."""
import os
import socket

import pytest

from ray_core.constants import MODEL_KEY
from ray_core.runner import run
from ray_core.spec import parse_spec
from tests.support import MemStore, detection_rows, fake_get, spec_text

pytestmark = pytest.mark.online


TASK, SMALL_BASE_MODEL = "object-detection", "PekingU/rtdetr_v2_r18vd"  # manifest.json, small preset


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


def test_the_small_presets_real_checkpoint_trains_and_exports():
    rows, files = detection_rows()
    store = MemStore()
    spec = parse_spec(spec_text(TASK, SMALL_BASE_MODEL, rows))
    spec = type(spec)(**{**spec.__dict__, "base_revision": None, "image_size": 320})
    report = run(spec, store, fake_get(rows, files))
    assert report["status"] == "succeeded", report["message"]
    assert len(store.puts[MODEL_KEY]) > 1_000_000
