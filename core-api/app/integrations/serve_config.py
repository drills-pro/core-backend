"""The declarative desired state of the shared predictor apps: settings plus the stable ray_serving package in, the Serve
config out. Pure (no I/O), so it is what the tests pin and what serve_client.ensure_apps compares the live config with.

The shape is Ray's ServeDeploySchema (ServeApplicationSchema per app); core-api builds it as plain dicts because the Ray SDK
is not a core-api dependency (it would bring the whole of `ray` into this image).
"""
from enum import StrEnum
from typing import Any

from app.core.settings import Serving, settings

SERVE_HTTP_PORT = 8000  # Serve's proxy port on every node
PREDICTOR_IMPORT_PATH = "ray_serving.app:build"
PREDICTOR_DEPLOYMENT = "Predictor"  # the class name inside ray_serving.app.build
PACKAGE_SHA_ENV = "RAY_SERVING_SHA256"  # in each app's runtime_env: which package the app was declared with
URL_ISSUED_ENV = "RAY_SERVING_URL_ISSUED_AT"  # unix seconds the zip's presigned URL was made; it is re-declared after half its life
PROXY_LOCATION = "EveryNode"  # a proxy on every node, so losing the head does not take the proxies


class ComputeTarget(StrEnum):
    """Where a deployment runs; also the project setting `compute_target`."""

    CPU = "cpu"
    GPU = "gpu"


def predictor_app(target: ComputeTarget) -> str:
    return f"predictor-{target.value}"


def render(package_url: str, sha256: str, issued_at: int, cfg: Serving | None = None) -> dict[str, Any]:
    """Both predictor apps. Replicas scale between the target's min and max, take `max_ongoing_requests` at a time, are health
    checked and get `graceful_shutdown_timeout_s` to finish what they are answering; each keeps `max_models_per_replica` models."""
    cfg = cfg or settings.serving

    def app(target: ComputeTarget) -> dict[str, Any]:
        size = getattr(cfg.apps, target.value)
        actor: dict[str, Any] = {"num_cpus": size.num_cpus, "num_gpus": size.num_gpus}
        if size.resources:
            actor["resources"] = size.resources  # pins the replicas to a worker pool (the CPU pool's custom resource)
        return {
            "name": predictor_app(target), "route_prefix": f"/{predictor_app(target)}", "import_path": PREDICTOR_IMPORT_PATH,
            "args": {"max_models_per_replica": cfg.max_models_per_replica},
            "runtime_env": {"py_modules": [package_url], "env_vars": {PACKAGE_SHA_ENV: sha256, URL_ISSUED_ENV: str(issued_at)}},
            "deployments": [{
                "name": PREDICTOR_DEPLOYMENT, "max_ongoing_requests": cfg.max_ongoing_requests,
                "autoscaling_config": {"min_replicas": size.min_replicas, "max_replicas": size.max_replicas},
                "health_check_period_s": cfg.health_check_period_seconds, "health_check_timeout_s": cfg.health_check_timeout_seconds,
                "graceful_shutdown_timeout_s": cfg.graceful_shutdown_timeout_seconds, "ray_actor_options": actor}]}

    return {
        "proxy_location": PROXY_LOCATION, "http_options": {"host": "0.0.0.0", "port": SERVE_HTTP_PORT},
        "applications": [app(t) for t in ComputeTarget]}


def _comparable(app: dict[str, Any]) -> dict[str, Any]:
    """An app without what changes on every render (the presigned URL, when it was made)."""
    runtime_env = {**app.get("runtime_env", {})}
    runtime_env.pop("py_modules", None)
    runtime_env["env_vars"] = {k: v for k, v in runtime_env.get("env_vars", {}).items() if k != URL_ISSUED_ENV}
    return {**app, "runtime_env": runtime_env}


def _contains(live: Any, wanted: Any) -> bool:
    """Everything in `wanted` is in `live` with the same value; Serve may add defaults of its own to what it reports."""
    if isinstance(wanted, dict):
        return isinstance(live, dict) and all(k in live and _contains(live[k], v) for k, v in wanted.items())
    if isinstance(wanted, list):
        return isinstance(live, list) and len(live) == len(wanted) and all(_contains(a, b) for a, b in zip(live, wanted))
    return live == wanted


def up_to_date(live: dict[str, Any], wanted: dict[str, Any], now: float, ttl_seconds: int) -> bool:
    """The live Serve applications (GET's `applications`) already match the desired config, and the zip URL they were declared
    with has most of its life left, so a replica started later can still fetch the package."""
    for app in wanted["applications"]:
        declared = (live.get(app["name"]) or {}).get("deployed_app_config") or {}
        issued = int(declared.get("runtime_env", {}).get("env_vars", {}).get(URL_ISSUED_ENV, 0))
        if now - issued > ttl_seconds / 2 or not _contains(_comparable(declared), _comparable(app)):
            return False
    return True
