"""Smoke job of a task bundle (planning/25): run the bundle's own smoke function on this core, both zips on the path.

    python -m ray_training.entrypoints.bundle_smoke '{"job_id": ..., "bucket": ..., "prefix": ..., "smoke": "module:function"}'

The function is given this spec (a dict) and returns whether it passed; raising counts as failed. Writes smoke.json
({"ok": bool}) under the prefix; exit code 1 when it failed.
"""
import json
import sys
import traceback

from ray_training.constants import SMOKE_KEY
from ray_training.registry import trainer_for
from ray_training.store import store_from_env


def main(argv: list[str]) -> int:
    arg = json.loads(argv[1])
    try:
        ok = bool(trainer_for(arg["smoke"])(arg))
    except Exception:  # noqa: BLE001 - any failure of the bundle is a failed smoke, written down below
        traceback.print_exc()
        ok = False
    store_from_env(arg["bucket"], arg["prefix"]).put(SMOKE_KEY, json.dumps({"ok": ok}).encode())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
