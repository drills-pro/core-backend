"""Scanners for the lines a training job prints (they work on a partial log too)."""
import json


def parse_metrics(logs: str) -> dict[str, float]:
    metrics = {}
    for line in logs.splitlines():
        if line.startswith("metric:"):
            key, _, value = line.removeprefix("metric:").partition("=")
            try:
                metrics[key] = float(value)
            except ValueError:
                continue
    return metrics


def parse_extra_metrics(logs: str) -> dict:
    """Extracts the `metrics_json:{...}` line every backend prints —
    per-epoch loss/accuracy history and a test-set confusion matrix, as
    opposed to parse_metrics' single scalar values."""
    for line in reversed(logs.splitlines()):
        if line.startswith("metrics_json:"):
            try:
                return json.loads(line.removeprefix("metrics_json:"))
            except ValueError:
                continue
    return {}


def parse_model_artifact(logs: str) -> dict | None:
    """Extracts the `model:{...json...}` line the numpy backend prints
    (weights it actually fit) — the servable artifact for deployments
    (req/04 Deployments group, req/07 FR3)."""
    for line in logs.splitlines():
        if line.startswith("model:"):
            try:
                return json.loads(line.removeprefix("model:"))
            except ValueError:
                continue
    return None
