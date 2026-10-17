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
REQUIRED: dict[str, tuple[str, ...]] = {
    "lakefs": ("endpoint", "access_key", "secret_key"),
    "labelstudio": ("url", "refresh_token"),
    "zitadel": ("USER_ID", "KEY_ID", "PRIVATE_KEY"),
    "zitadel_oidc": (),
}

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
    missing = [k for k in REQUIRED[name] if k not in data]
    if missing:
        raise SecretError(f"secret {name!r}: {path} is missing {missing}")
    _cache[name] = (path, mtime, data)
    return data
