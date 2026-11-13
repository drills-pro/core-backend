"""A task bundle's smoke runs its own function on this core and writes smoke.json; a raise is a failed smoke, not a crash."""
import json
import sys
import types

import pytest

from ray_training.constants import SMOKE_KEY
from ray_training.entrypoints import bundle_smoke
from tests.support import MemStore


def _boom(arg):
    raise RuntimeError("bundle broke")


@pytest.mark.parametrize("fn, ok", [(lambda arg: arg["job_id"] == "j", True), (lambda arg: False, False), (_boom, False)])
def test_the_bundle_smoke_writes_whether_it_passed(monkeypatch, fn, ok):
    monkeypatch.setitem(sys.modules, "fakebundle", types.SimpleNamespace(run=fn))
    store = MemStore()
    monkeypatch.setattr(bundle_smoke, "store_from_env", lambda bucket, prefix: store)
    spec = {"job_id": "j", "bucket": "b", "prefix": "p/", "smoke": "fakebundle:run"}
    assert bundle_smoke.main(["x", json.dumps(spec)]) == (0 if ok else 1)
    assert json.loads(store.puts[SMOKE_KEY]) == {"ok": ok}
