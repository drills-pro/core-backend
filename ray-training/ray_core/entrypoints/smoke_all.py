"""Smoke job of a package that holds ray_core (planning/25 "Core and bundles are gated together"): every bundle given runs
its own smoke (entrypoints.smoke) on this core, each in a driver process of its own and under a prefix of its own.

    python -m ray_core.entrypoints.smoke_all '{"job_id": ..., "bucket": ..., "prefix": ..., "bundles": [{"smoke": ..., "requires": [...]}]}'

core-api lists the stable bundles when it smokes a core (the core x bundle matrix in one job), and the one bundle when it
smokes a bundle; every bundle's zip is a py_module of the job, so the processes inherit it on PYTHONPATH. Writes smoke.json
({"ok": bool}, true only when every bundle passed) under the prefix; exit code 1 when one failed.
"""
import json
import subprocess
import sys

from ray_core.constants import SMOKE_KEY
from ray_core.store import store_from_env

SMOKE_MODULE = "ray_core.entrypoints.smoke"


def main(argv: list[str]) -> int:
    arg = json.loads(argv[1])
    ok = True
    for i, bundle in enumerate(arg["bundles"]):
        one = {**arg, **bundle, "job_id": f"{arg['job_id']}-{i}", "prefix": f"{arg['prefix']}{i}/"}
        one.pop("bundles")
        ok = subprocess.run([sys.executable, "-m", SMOKE_MODULE, json.dumps(one)]).returncode == 0 and ok
    store_from_env(arg["bucket"], arg["prefix"]).put(SMOKE_KEY, json.dumps({"ok": ok}).encode())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
