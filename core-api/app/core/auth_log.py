"""Sign-in log records, pushed over OTLP to Loki (the otel-gateway in prod, the LGTM container locally).

Only this module's logger goes out that way, and it does not propagate to the root logger: stdout is
tailed into Loki by the cluster's otel-agent too, so propagating would ship every record twice. Each
record carries the active span's trace and span ids, so Grafana links it to the request in Tempo.

Sign-out happens at Zitadel (/oidc/v1/end_session) and never reaches this process; Zitadel ships a
request log for every request it serves, so query its sign-in, sign-up and sign-out paths in Grafana.

In Grafana: {service_name="core-backend"} | auth_event="sign_in". Attribute dots become underscores.
"""
import logging
import socket

from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource

from app.core.enums import AuthEvent, AuthResult
from app.core.settings import SERVICE_NAME, settings

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)  # the root logger stays at WARNING; sign-ins are INFO

EVENT_ATTRIBUTE = "auth.event"
RESULT_ATTRIBUTE = "auth.result"
USER_ATTRIBUTE = "user.id"  # Zitadel's sub, an opaque id; never the email


def setup() -> None:
    if settings.env == "test":
        return  # records stay ordinary ones that caplog sees; nothing tries to reach a collector
    resource = Resource.create({"service.name": SERVICE_NAME, "service.instance.id": socket.gethostname()})
    provider = LoggerProvider(resource=resource)
    exporter = OTLPLogExporter(endpoint=settings.observability.otlp_endpoint, insecure=True)
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=provider))
    logger.propagate = False


def signed_in(subject: str, *, first_time: bool) -> None:
    """A session reached /v1/me, which the SPA calls right after login (and again on each reload)."""
    event = AuthEvent.SIGN_UP if first_time else AuthEvent.SIGN_IN
    logger.info(event.value, extra={EVENT_ATTRIBUTE: event.value, USER_ATTRIBUTE: subject})


def token_rejected(result: AuthResult) -> None:
    event = AuthEvent.TOKEN_REJECTED
    logger.warning("%s: %s", event.value, result.value, extra={EVENT_ATTRIBUTE: event.value, RESULT_ATTRIBUTE: result.value})
