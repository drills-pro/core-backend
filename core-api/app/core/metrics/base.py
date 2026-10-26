"""The provider and the helpers every metrics module defines its instruments with."""
import re
import socket

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import Counter, Histogram
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

from app.core.settings import SERVICE_NAME, settings

PREFIX = f"drills.{SERVICE_NAME.replace('-', '_')}"
NAME_PATTERN = re.compile(r"^drills(\.[a-z][a-z0-9_]*){3,}$")  # drills.<service>.<namespace>[.<sub>...].<name>
EXPORT_INTERVAL_MILLIS = 15_000


def _build_provider() -> MeterProvider:
    # service.instance.id (the pod name) becomes Mimir's `instance` label, keeping replicas' series apart.
    resource = Resource.create({"service.name": SERVICE_NAME, "service.instance.id": socket.gethostname()})
    if settings.env == "test":
        return MeterProvider(resource=resource)  # no reader: tests never try to reach a collector
    exporter = OTLPMetricExporter(endpoint=settings.observability.otlp_endpoint, insecure=True)
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=EXPORT_INTERVAL_MILLIS)
    return MeterProvider(resource=resource, metric_readers=[reader])


metrics.set_meter_provider(_build_provider())
_meter = metrics.get_meter(SERVICE_NAME)


def _full_name(name: str) -> str:
    full = f"{PREFIX}.{name}"
    if not NAME_PATTERN.match(full):
        raise ValueError(f"metric name {full!r} must be {PREFIX}.<namespace>[.<sub_namespace>...].<snake_case_name>")
    return full


def counter(name: str, description: str) -> Counter:
    return _meter.create_counter(_full_name(name), description=description)


def histogram(name: str, description: str, unit: str) -> Histogram:
    return _meter.create_histogram(_full_name(name), unit=unit, description=description)

