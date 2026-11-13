"""Smoke job: a bundle's tiny Ray Train run through the real train driver, to check a bundle (or a ray_core) before it is promoted.

    python -m ray_core.entrypoints.smoke '{"job_id": ..., "bucket": ..., "prefix": ..., "smoke": "imgcls.smoke:spec_doc"}'

`smoke` is the bundle manifest's `smoke`: a function that takes this argument and returns a train job spec whose data travels as
`data:` URLs (store.data_url), so any node can read it and no base model is downloaded. `requires` (optional, the manifest's) are
distributions that must be installed here: one that is not fails the smoke, nothing is installed. STAGING_* in the environment. Writes
smoke.json ({"ok": bool}) under the prefix; exit code 1 when the run failed.
"""
import json
import logging
import sys
from importlib import metadata

from ray_core import registry
from ray_core.constants import SMOKE_KEY
from ray_core.entrypoints import train
from ray_core.errors import Unsupported
from ray_core.store import store_from_env

logger = logging.getLogger(__name__)


def check_requires(dists: list[str]) -> None:
    missing = []
    for dist in dists:
        try:
            metadata.version(dist)
        except metadata.PackageNotFoundError:
            missing.append(dist)
    if missing:
        raise Unsupported(f"not installed in this image: {', '.join(missing)}")


def main(argv: list[str]) -> int:
    arg = json.loads(argv[1])
    try:
        check_requires(arg.get("requires", []))
        doc = registry.load(arg["smoke"])(arg)
    except Exception:  # noqa: BLE001 - a bundle that is missing a library or cannot even build a spec has failed its smoke
        logger.exception("smoke %r did not start", arg.get("smoke"))
        code = 1
    else:
        code = train.main([argv[0], json.dumps(doc)])
    store_from_env(arg["bucket"], arg["prefix"]).put(SMOKE_KEY, json.dumps({"ok": code == 0}).encode())
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
