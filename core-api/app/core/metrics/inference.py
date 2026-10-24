"""drills.core_backend.inference.*: the signal behind req/08 FR2's p99-vs-SLA alert.
Database size and GPU quota are not ours: they come from the Postgres exporter and kube-state-metrics."""
from app.core.metrics.base import histogram

latency = histogram("inference.latency", "Real /predict request latency by deployment", unit="ms")
