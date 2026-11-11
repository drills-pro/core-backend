"""Secrets, read from the JSON files named in settings.secrets.

In the cluster the Vault Agent Injector writes each file (k8s/apps/backend/deployment.yaml) and
rewrites it on rotation; locally scripts/secrets-init.sh and the bootstrap scripts write the same
shape under .local/secrets/. A file is re-read whenever its mtime changes, so rotation needs no
restart and the cost of calling get() per use is one stat().
"""
import json
from pathlib import Path

from app.core.settings import settings

# name -> keys every consumer relies on; a file missing one fails here, with the path, not as a
# KeyError deep in a request.
LAKEFS = "lakefs"
STAGING = "staging"
INGEST = "ingest"  # the Ray ingest job's own credential: the staging bucket only, never the lakeFS blockstore
# Read only by app/integrations/data_plane, which also reports their health (tests/architecture).
DATA_PLANE = (LAKEFS, STAGING, INGEST)

REQUIRED: dict[str, tuple[str, ...]] = {
    "db": ("username", "password"),
    LAKEFS: ("endpoint", "access_key", "secret_key"),
    STAGING: ("endpoint", "access_key", "secret_key"),
    INGEST: ("endpoint", "access_key", "secret_key"),
    "zitadel": ("USER_ID", "KEY_ID", "PRIVATE_KEY"),
}

# Not in REQUIRED, so readiness does not depend on them: the feature that reads one is off while its file is missing.
OPTIONAL: dict[str, tuple[str, ...]] = {"webhooks": ("signing_key",)}

_cache: dict[str, tuple[Path, int, dict]] = {}


class SecretError(RuntimeError):
    pass


def get(name: str) -> dict:
    path: Path = getattr(settings.secrets, name)
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError as e:
        raise SecretError(f"secret {name!r}: no file at {path}") from e

    hit = _cache.get(name)
    if hit and hit[0] == path and hit[1] == mtime:
        return hit[2]
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise SecretError(f"secret {name!r}: {path} is not valid JSON") from e
    missing = [k for k in {**REQUIRED, **OPTIONAL}[name] if k not in data]
    if missing:
        raise SecretError(f"secret {name!r}: {path} is missing {missing}")
    _cache[name] = (path, mtime, data)
    return data


def problems(skip: tuple[str, ...] = ()) -> list[str]:
    """Why each required secret file (except `skip`) does not load; empty when all are fine."""
    bad = []
    for name in REQUIRED:
        if name in skip:
            continue
        try:
            get(name)
        except SecretError as e:
            bad.append(str(e))
    return bad
