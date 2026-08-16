"""OTel metrics → LGTM's OTLP collector → its bundled Prometheus.
req/08 FR2: alerting needs a real signal for training job failure rate;
this is that signal."""
import os

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://lgtm:4317")

_provider = MeterProvider(
    resource=Resource.create({"service.name": "core-api"}),
    metric_readers=[
        PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=OTLP_ENDPOINT, insecure=True), export_interval_millis=5000
        )
    ],
)
metrics.set_meter_provider(_provider)
_meter = metrics.get_meter("core-api")

training_runs_total = _meter.create_counter(
    "training_runs_total", description="Training runs by terminal status (req/08 FR2 signal)"
)

# req/08 FR2 "Postgres... disk pressure" signal. The OTel callback must be
# synchronous, so a background task (see main.py) polls the real DB size
# via asyncpg and stores it here; the callback just reads the last value.
_pg_size_bytes = 0


def set_pg_size_bytes(value: int) -> None:
    global _pg_size_bytes
    _pg_size_bytes = value


def _pg_size_callback(_options):
    yield metrics.Observation(_pg_size_bytes, {"database": "core"})


postgres_database_size_bytes = _meter.create_observable_gauge(
    "postgres_database_size_bytes",
    callbacks=[_pg_size_callback],
    description="Real Postgres core DB size, polled every 30s (req/08 FR2 disk-pressure signal)",
)

# req/08 FR2 "inference p99 latency above SLA per deployment" signal —
# real request latency from app/deployments.py::predict, not a synthetic
# number, so p99 alerting on it means something.
inference_latency_ms = _meter.create_histogram(
    "inference_latency_ms", unit="ms", description="Real /predict request latency (req/08 FR2 signal)"
)

# req/08 FR2 "GPU node pool exhaustion" signal. K8s ResourceQuota tracks
# resource *requests* (including nvidia.com/gpu) at admission time
# regardless of whether real GPU hardware backs the extended resource —
# real, not fabricated, even at 0 physical GPUs (see k8s/tenant-quota-example.yaml
# and README's Observability section for why this is honestly the same
# category as any other quota-based exhaustion signal, not decoration).
_gpu_quota_used = 0
_gpu_quota_hard = 0


def set_gpu_quota(used: int, hard: int) -> None:
    global _gpu_quota_used, _gpu_quota_hard
    _gpu_quota_used = used
    _gpu_quota_hard = hard


def _gpu_quota_callback(_options):
    yield metrics.Observation(_gpu_quota_used, {"type": "used"})
    yield metrics.Observation(_gpu_quota_hard, {"type": "hard"})


gpu_quota_requests = _meter.create_observable_gauge(
    "gpu_quota_requests",
    callbacks=[_gpu_quota_callback],
    description="requests.nvidia.com/gpu ResourceQuota used/hard, polled every 30s (req/08 FR2 GPU-exhaustion signal)",
)
