import json
import os

import hvac

from app.core import config

VAULT_ADDR = config.VAULT_ADDR
VAULT_TOKEN_PATH = "/vault/secrets/token"
APP_BACKEND_SECRETS_DIR = "/var/app-secret/app-backend"


def _token() -> str:
    """The Vault Agent Injector sidecar keeps this file's k8s-auth token
    continuously renewed; local dev has no injector, so fall back to the
    static VAULT_TOKEN env var."""
    try:
        with open(VAULT_TOKEN_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        return config.VAULT_TOKEN


def _client() -> hvac.Client:
    return hvac.Client(url=VAULT_ADDR, token=_token())


def get_kv(name: str) -> dict:
    """This app's own secret (app-secret/app-backend/<name> in Vault). Every such secret is
    already rendered to /var/app-secret/app-backend/<name> as JSON by the cluster's Vault Agent
    Injector sidecar (k8s/apps/backend/deployment.yaml's writeToFile template) — read that
    instead of round-tripping to Vault's API on every call. Local dev has no injector sidecar,
    so it falls back to a live read of the same app-secret/app-backend/<name> path."""
    try:
        with open(os.path.join(APP_BACKEND_SECRETS_DIR, name)) as f:
            return json.load(f)
    except FileNotFoundError:
        resp = _client().secrets.kv.v2.read_secret_version(path=f"app-backend/{name}", mount_point="app-secret")
        return resp["data"]["data"]


def get_db_creds(role: str = "core-app") -> tuple[str, str, int]:
    """Issue a short-lived Postgres role from Vault's database secrets engine.

    Returns (username, password, lease_seconds).
    """
    resp = _client().secrets.database.generate_credentials(name=role)
    data = resp["data"]
    return data["username"], data["password"], resp["lease_duration"]
