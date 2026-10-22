"""OTel metrics -> Prometheus, scraped, not pushed.
req/08 FR2: alerting needs a real signal for training job failure rate;
this is that signal.

ponytail: this used to push to the same OTLP endpoint as traces, but that
endpoint is Tempo (traces-only) — it has no metrics service, so every push
failed with UNIMPLEMENTED. This cluster's real metrics backend is
Prometheus, which scrapes rather than receives pushes, so PrometheusMetricReader
(the OTel-maintained bridge into prometheus_client's default registry) is the
correct reader here, not another push exporter. See app/main.py's /metrics
mount for the scrape endpoint.

The sign-in metrics at the bottom are the exception: they are pushed over OTLP to
observability.metrics_otlp_endpoint (the cluster now has an OTel gateway in front of Mimir).
ponytail: the rest move to the push provider feature by feature; then the scrape job and /metrics go.
"""
import socket

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

from app.core.settings import settings

_provider = MeterProvider(
    resource=Resource.create({"service.name": "core-api"}),
    metric_readers=[PrometheusMetricReader()],
)
metrics.set_meter_provider(_provider)
_meter = metrics.get_meter("core-api")

# Not the global provider, so these instruments never show up on /metrics as well (counted twice).
# service.instance.id (the pod name) becomes Mimir's `instance` label, keeping replicas' series apart.
_push_provider = MeterProvider(
    resource=Resource.create({"service.name": "core-api", "service.instance.id": socket.gethostname()}),
    metric_readers=[
        PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=settings.observability.metrics_otlp_endpoint, insecure=True),
            export_interval_millis=15_000,
        )
    ],
)
_push_meter = _push_provider.get_meter("core-api")

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
# real request latency from app/modules/deployments/routes.py::predict, not a synthetic
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

# Sign-in health. Login and logout happen at Zitadel (core/auth.py); what this process sees is every
# bearer token it checks, each identity it meets for the first time, and who has been seen lately.
# Pushed, not scraped (see the module docstring).
auth_token_verifications_total = _push_meter.create_counter(
    "auth_token_verifications_total", description="Bearer token checks by result (enums.AuthResult)"
)
auth_principals_created_total = _push_meter.create_counter(
    "auth_principals_created_total", description="Zitadel identities seen for the first time (sign-ups)"
)
zitadel_userinfo_total = _push_meter.create_counter(
    "zitadel_userinfo_total", description="Zitadel userinfo lookups by result (enums.UserinfoResult)"
)

# Principals whose last_seen_at (bumped by /v1/me) falls inside each window, polled every 30s by
# background.py like the DB size above.
ACTIVE_PRINCIPAL_WINDOW_DAYS = {"1d": 1, "7d": 7, "30d": 30}
_active_principals: dict[str, int] = {}


def set_active_principals(counts: dict[str, int]) -> None:
    global _active_principals
    _active_principals = counts


def _active_principals_callback(_options):
    for window, count in _active_principals.items():
        yield metrics.Observation(count, {"window": window})


auth_active_principals = _push_meter.create_observable_gauge(
    "auth_active_principals",
    callbacks=[_active_principals_callback],
    description="Distinct principals seen via /v1/me within each window (1d/7d/30d)",
)
