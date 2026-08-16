"""OpenTelemetry tracing → LGTM's Tempo, via the LGTM stack's built-in OTLP
collector (req/08-observability-lgtm.md: "instrument app backend... at
minimum"). FR1's tenant_id/project_id label convention is applied via
`tag_current_span` at the points that resolve them (see app/tenancy.py).
"""
import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://lgtm:4317")


def setup(app) -> None:
    provider = TracerProvider(resource=Resource.create({"service.name": "core-api"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT, insecure=True)))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()
    AsyncPGInstrumentor().instrument()


def tag_current_span(**attributes: str) -> None:
    span = trace.get_current_span()
    for key, value in attributes.items():
        span.set_attribute(key, value)
