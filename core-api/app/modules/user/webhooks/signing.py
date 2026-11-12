"""Webhook signatures. The endpoint secret is derived, not stored: HMAC(master key, endpoint id)."""
import hashlib
import hmac
import ipaddress
import uuid
from urllib.parse import urlsplit

from app.core import secret_files
from app.modules.user.webhooks import constants as c


class SigningUnavailable(RuntimeError):
    """The webhooks secret file is missing or malformed: nothing can be signed."""


def master_key() -> bytes:
    try:
        return secret_files.get(c.SIGNING_SECRET_FILE)[c.SIGNING_KEY_FIELD].encode()
    except secret_files.SecretError as e:
        raise SigningUnavailable(str(e)) from e


def endpoint_secret(endpoint_id: uuid.UUID) -> str:
    return hmac.new(master_key(), str(endpoint_id).encode(), hashlib.sha256).hexdigest()


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"{c.SIGNATURE_VERSION}={mac}"


def blocked_address(host: str) -> bool:
    """A literal address that is not publicly routable (loopback, private, link-local, ...)."""
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def url_problem(url: str) -> str | None:
    """Why `url` cannot be a webhook target, or None. Names are checked here, addresses again when delivering."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or parts.username or parts.password:
        return "the url must be https, with a host and no credentials"
    if host == "localhost" or host.endswith(c.BLOCKED_HOST_SUFFIXES) or blocked_address(host):
        return "the url must point at a public host"
    return None
