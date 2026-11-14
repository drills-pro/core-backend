"""The generic smoke entrypoint: a bundle's smoke spec runs through the driver; a smoke that cannot start is a failed smoke, not a
missing smoke.json. And the session, not torch, decides the device a run trains on."""
import json

import pytest

from ray_core.constants import SMOKE_KEY
from ray_core.runner import run
from ray_core.spec import parse_spec
from tests.support import MemStore, classification_rows, fake_get, spec_text


@pytest.mark.parametrize("arg", [{"smoke": "tests.nope:spec"}, {"smoke": "tests.tiny:train", "requires": ["no-such-distribution"]}])
def test_a_smoke_that_cannot_start_writes_ok_false(tmp_path, monkeypatch, arg):
    smoke = pytest.importorskip("ray_core.entrypoints.smoke", exc_type=ImportError)  # imports the driver, so ray.train
    monkeypatch.setenv("STAGING_ENDPOINT", f"file://{tmp_path}")
    code = smoke.main(["smoke", json.dumps({"job_id": "j", "bucket": "b", "prefix": "p/", **arg})])
    assert code == 1 and json.loads((tmp_path / "p" / SMOKE_KEY).read_text()) == {"ok": False}


def test_the_session_decides_the_device_the_model_trains_on(monkeypatch):
    # a Ray Train worker without a GPU of its own trains on cpu even where torch sees cuda (the smoke job on the GPU node)
    from ray_core import loop

    asked = []

    class Cpu(loop.Session):
        def device(self):
            asked.append(True)
            return loop.torch.device("cpu")

    monkeypatch.setattr(loop, "device", lambda: loop.torch.device("cuda"))
    monkeypatch.setattr(loop, "session", Cpu())
    rows, files = classification_rows()
    assert run(parse_spec(spec_text("image-classification", "m", rows)), MemStore(), fake_get(rows, files))["status"] == "succeeded" and asked
