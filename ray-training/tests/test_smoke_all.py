"""A core's smoke runs every bundle's own smoke in a process of its own and passes only when all of them did."""
import json
from types import SimpleNamespace

import pytest

from ray_core.constants import SMOKE_KEY
from ray_core.entrypoints import smoke_all
from tests.support import MemStore


@pytest.mark.parametrize("codes, ok", [([], True), ([0, 0], True), ([0, 1], False)])
def test_every_bundle_is_smoked_under_its_own_prefix(monkeypatch, codes, ok):
    runs, store = [], MemStore()

    def run(argv):
        runs.append(json.loads(argv[-1]))
        return SimpleNamespace(returncode=codes[len(runs) - 1])

    monkeypatch.setattr(smoke_all.subprocess, "run", run)
    monkeypatch.setattr(smoke_all, "store_from_env", lambda bucket, prefix: store)
    bundles = [{"smoke": f"b{i}.smoke:spec_doc", "requires": []} for i in range(len(codes))]
    arg = {"job_id": "j", "bucket": "b", "prefix": "p/", "bundles": bundles}
    assert smoke_all.main(["x", json.dumps(arg)]) == (0 if ok else 1)
    assert [(r["smoke"], r["prefix"], r["job_id"], "bundles" in r) for r in runs] == [
        (b["smoke"], f"p/{i}/", f"j-{i}", False) for i, b in enumerate(bundles)]
    assert json.loads(store.puts[SMOKE_KEY]) == {"ok": ok}
