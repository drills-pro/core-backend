"""Vault issues core-api's Postgres credentials (database secrets engine). Every static secret is a
mounted file instead, see app/core/secret_files.py."""
import hvac

from app.core.settings import settings


def _client() -> hvac.Client:
    # Re-read per call: the Vault Agent Injector keeps this token file continuously renewed.
    return hvac.Client(url=settings.vault.addr, token=settings.vault.token_file.read_text().strip())


def is_authenticated() -> bool:
    return _client().is_authenticated()


def get_db_creds(role: str = "core-app") -> tuple[str, str, int]:
    """Issue a short-lived Postgres role from Vault's database secrets engine.

    Returns (username, password, lease_seconds).
    """
    resp = _client().secrets.database.generate_credentials(name=role)
    data = resp["data"]
    return data["username"], data["password"], resp["lease_duration"]
