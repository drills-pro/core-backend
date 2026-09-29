import hvac

from app.core import config

VAULT_ADDR = config.VAULT_ADDR
VAULT_TOKEN_PATH = "/vault/secrets/token"


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


def get_kv(path: str) -> dict:
    resp = _client().secrets.kv.v2.read_secret_version(path=path, mount_point=config.VAULT_KV_MOUNT)
    return resp["data"]["data"]


def get_db_creds(role: str = "core-app") -> tuple[str, str, int]:
    """Issue a short-lived Postgres role from Vault's database secrets engine.

    Returns (username, password, lease_seconds).
    """
    resp = _client().secrets.database.generate_credentials(name=role)
    data = resp["data"]
    return data["username"], data["password"], resp["lease_duration"]
