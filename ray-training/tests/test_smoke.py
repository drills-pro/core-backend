"""The smoke job's spec and trainer, in one process (the Ray run is the job's own check)."""
import json

from ray_training.constants import MODEL_KEY, REPORT_KEY
from ray_training.entrypoints.smoke import spec_doc
from ray_training.runner import run
from ray_training.spec import parse_spec
from ray_training.store import http_get
from tests.support import MemStore


def test_the_smoke_spec_trains_and_exports_without_a_base_model():
    spec = parse_spec(json.dumps(spec_doc({"job_id": "j", "bucket": "b", "prefix": "p/"})))
    store = MemStore()
    report = run(spec, store, http_get)  # data: URLs are served by urlopen
    assert report["status"] == "succeeded", report["message"]
    assert len(report["history"]) == spec.epochs and MODEL_KEY in store.puts and REPORT_KEY in store.puts
