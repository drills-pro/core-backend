import os

import hvac

VAULT_ADDR = os.environ.get("VAULT_ADDR", "http://vault:8200")
VAULT_TOKEN = os.environ.get("VAULT_TOKEN", "root")

_client = hvac.Client(url=VAULT_ADDR, token=VAULT_TOKEN)


def get_kv(path: str) -> dict:
    resp = _client.secrets.kv.v2.read_secret_version(path=path)
    return resp["data"]["data"]


def get_db_creds(role: str = "core-app") -> tuple[str, str, int]:
    """Issue a short-lived Postgres role from Vault's database secrets engine.

    Returns (username, password, lease_seconds).
    """
    resp = _client.secrets.database.generate_credentials(name=role)
    data = resp["data"]
    return data["username"], data["password"], resp["lease_duration"]
