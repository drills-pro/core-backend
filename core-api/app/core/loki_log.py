"""Ships chosen loggers' records over OTLP to Loki (the otel-gateway in prod, the LGTM container locally).

Each ship() call is its own OTLP stream: its own LoggerProvider whose resource carries the service.name given, which
Loki indexes as the service_name label. The loggers passed stop propagating to the root logger, so their records go
to OTLP only: stdout is tailed into Loki by the cluster's otel-agent too, and propagating would ship every record
twice, under the wrong stream. Each record carries the active span's trace and span ids, so Grafana links it to the
request in Tempo.

In Grafana: {service_name="<service name>"} | <attribute>="<value>". Attribute dots become underscores.
"""
import logging
import socket

from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource

from app.core.settings import settings


def ship(service_name: str, *loggers: logging.Logger) -> None:
    if settings.env == "test":
        return  # records stay ordinary ones that caplog sees; nothing tries to reach a collector
    resource = Resource.create({"service.name": service_name, "service.instance.id": socket.gethostname()})
    provider = LoggerProvider(resource=resource)
    exporter = OTLPLogExporter(endpoint=settings.observability.otlp_endpoint, insecure=True)
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    handler = LoggingHandler(level=logging.INFO, logger_provider=provider)
    for logger in loggers:
        logger.addHandler(handler)
        logger.propagate = False
