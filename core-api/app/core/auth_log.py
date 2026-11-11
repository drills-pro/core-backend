"""Sign-in log records, an OTLP stream under the backend's service name (core/loki_log.py ships it).

Sign-out happens at Zitadel (/oidc/v1/end_session) and never reaches this process; Zitadel ships a
request log for every request it serves, so query its sign-in, sign-up and sign-out paths in Grafana.

In Grafana: {service_name="core-backend"} | auth_event="sign_in". Attribute dots become underscores.
"""
import logging

from app.core import loki_log
from app.core.enums import AuthAttribute, AuthEvent, AuthResult
from app.core.settings import SERVICE_NAME

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)  # the root logger stays at WARNING; sign-ins are INFO


def setup() -> None:
    loki_log.ship(SERVICE_NAME, logger)


def signed_in(subject: str, event: AuthEvent) -> None:
    """A session reached /v1/me, which the SPA calls right after login (and again on each reload)."""
    logger.info(event.value, extra={AuthAttribute.EVENT: event.value, AuthAttribute.USER: subject})


def token_rejected(result: AuthResult) -> None:
    event = AuthEvent.TOKEN_REJECTED
    logger.warning("%s: %s", event.value, result.value, extra={AuthAttribute.EVENT: event.value, AuthAttribute.RESULT: result.value})
