"""Every non-secret knob core-api reads, loaded once from config/*.yaml.

Layers, lowest to highest precedence: config/base.yaml (shared defaults), config/<APP_ENV>.yaml
(everything specific to that environment, including its service hostnames), then
CORE_<SECTION>__<KEY> environment variables. APP_ENV (local | compose | test | prod) selects the overlay. It is required and has no default
here; the Docker image sets it to prod, so dev settings can never be picked up by accident.

Secrets are not here. `secrets:` holds the *path* of each JSON file; app/core/secret_files.py
reads them.
"""
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from app.core.enums import BlockstoreScheme

CORE_API_DIR = Path(__file__).resolve().parents[2]
CONFIG_DIR = CORE_API_DIR / "config"
SERVICE_NAME = "core-backend"  # OTel service.name of traces and metrics; metric names use it snake_cased
ENVIRONMENTS = ("local", "compose", "test", "prod")


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Pg(_Section):
    host: str
    port: int
    database: str
    # Per-process pool. Each leader-elected poller (app/background.py) holds one connection for the
    # length of its run, so leave headroom above the pollers' count; total across replicas must stay
    # under Postgres max_connections.
    pool_min: int
    pool_max: int
    command_timeout: float


class Zitadel(_Section):
    issuer: str  # the public URL: it is what tokens carry as `iss`
    internal_url: str  # where to fetch signing keys from instead (in-cluster Service); "" = issuer
    project_id: str


class Auth(_Section):
    dev_headers: bool


class Ray(_Section):
    dashboard: str
    max_concurrent_training_runs: int


class RayJobCfg(_Section):
    namespace: str
    cluster_name: str  # RayCluster to run jobs on (warm, shared); "" = an ephemeral cluster per job from cluster_spec
    cluster_spec: dict  # KubeRay rayClusterSpec, used when cluster_name is empty
    shutdown_after_finish: bool
    ttl_after_finished_seconds: int  # how long the CR (and so the job's status) outlives the job; reconcilers must fit inside


class Compute(_Section):
    backend: Literal["ray_dashboard", "rayjob"]  # app.integrations.compute.Backend
    rayjob: RayJobCfg


class Background(_Section):
    pollers_in_api: bool  # false once the worker Deployment (python -m app.worker) runs them


class Mlflow(_Section):
    url: str


class DataPlane(_Section):
    staging_bucket: str  # S3 bucket the ingest job writes to; core-api validates, then lakeFS imports from it
    lakefs_blockstore_bucket: str  # bucket lakeFS keeps repository storage namespaces in
    lakefs_blockstore_scheme: BlockstoreScheme  # must match the blockstore type lakeFS runs with
    presign_ttl_seconds: int
    commit_timeout_seconds: int  # how long commit_staged waits for the lakeFS import to finish


class DatasetLimits(_Section):
    """Ceilings the API and the ingest job enforce (docs/datasets-plan.md decision 16). Static knobs."""

    upload_max_bytes: int  # one archive or CSV, single presigned PUT (S3 caps that at 5 GB)
    extracted_max_bytes: int
    extracted_max_ratio: int  # extracted size / archive size above this is a zip bomb
    max_files: int
    max_archive_depth: int  # directory levels inside an archive
    max_path_length: int
    image_max_dimension: int  # pixels, per side
    image_max_bytes: int
    csv_min_rows: int
    csv_max_rows: int
    csv_max_columns: int
    hf_max_repo_bytes: int
    job_timeout_seconds: int
    ray_job_cpus: int
    ray_job_memory_bytes: int
    max_active_imports_per_tenant: int


class Training(_Section):
    """The driver and the deadline ceiling; the task's own cpu, memory and gpu are per preset (app/core/presets.py)."""

    driver_cpus: float  # the CPU-pool driver that starts that task
    max_runtime_seconds: int  # ceiling of every preset's max_runtime_s; presigned data reads outlive it


class ServingApp(_Section):
    """One shared predictor app (ray_serving), per compute target: its replicas and what each one holds."""

    min_replicas: int
    max_replicas: int
    num_cpus: float
    num_gpus: float
    resources: dict[str, float]  # custom Ray resources each replica asks for: how an app is pinned to a worker pool


class ServingApps(_Section):
    cpu: ServingApp
    gpu: ServingApp


class Serving(_Section):
    """The shared Serve apps every shared deployment is answered by (app/integrations/serve_client.py)."""

    max_models_per_replica: int  # models a replica keeps loaded; the least recently used is dropped for another
    max_ongoing_requests: int  # per replica; more wait in Serve's queue
    health_check_period_seconds: int  # how often Serve probes a replica
    health_check_timeout_seconds: int  # a replica that has not answered a probe in this long is replaced
    graceful_shutdown_timeout_seconds: int  # a stopping replica gets this long to finish its requests
    url: str  # the Serve proxy Service core-api calls (a ClusterIP Service in front of every node's proxy); "" = the Ray head's address
    connect_retries: int  # a call that cannot connect is tried again this many times
    request_timeout_seconds: int  # core-api's wait for an answer, which includes a cold load of the model
    max_body_bytes: int  # the ceiling: a /predict or test body larger than this is refused with 413 before it is parsed; also the most a plan may allow
    max_inflight_per_deployment: int  # fallback for a tier without a plan_limits row: concurrent requests to one deployment in one core-api process; more get 429
    predict_calls_retention_days: int  # predict_calls rows older than this are purged
    test_calls_retention_days: int  # deployment_test_calls rows (the try-it payloads) older than this are purged
    package_url_ttl_seconds: int  # how long the presigned URL of the ray_serving zip in the Serve config works (S3 caps it at 7 days)
    apps: ServingApps


class Observability(_Section):
    otlp_endpoint: str


class Public(_Section):
    lakefs_endpoint: str
    staging_endpoint: str  # browsers PUT uploads here


class SecretPaths(_Section):
    db: Path  # {"username", "password"} for Postgres; the Vault agent rewrites it on rotation
    lakefs: Path
    staging: Path  # {"endpoint", "access_key", "secret_key"} of the S3 store holding the staging bucket
    ingest: Path  # same shape as staging, but limited to the staging bucket: the ingest job runs hostile input with it
    zitadel: Path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CORE_", env_nested_delimiter="__", extra="forbid")

    env: str
    pg: Pg
    zitadel: Zitadel
    auth: Auth
    ray: Ray
    compute: Compute
    background: Background
    mlflow: Mlflow
    data_plane: DataPlane
    dataset_limits: DatasetLimits
    training: Training
    serving: Serving
    observability: Observability
    public: Public
    secrets: SecretPaths

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        env = init_settings.init_kwargs["env"]
        # One source per file, highest precedence first: pydantic-settings deep-merges across
        # sources, but a single multi-file source only merges the top level.
        yaml = lambda path: YamlConfigSettingsSource(settings_cls, yaml_file=path)  # noqa: E731
        layers = [yaml(CONFIG_DIR / f"{env}.yaml"), yaml(CONFIG_DIR / "base.yaml")]
        if env == "local":
            # Gitignored, per developer (scripts/steps/zitadel-restore.sh writes the stack's Zitadel project id here). A missing file is skipped.
            layers.insert(0, yaml(CONFIG_DIR / "local.override.yaml"))
        return (init_settings, env_settings, *layers)

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        if self.env == "prod":
            # Client-controlled headers must never stand in for a token in the cluster.
            if self.auth.dev_headers:
                raise ValueError("auth.dev_headers must not be enabled when APP_ENV=prod")
            relative = [n for n, p in self.secrets if not p.is_absolute()]
            if relative:
                raise ValueError(f"prod secret paths must be absolute: {relative}")
        # Relative paths (local dev) are relative to core-api/, not to the process's cwd.
        for name, path in self.secrets:
            setattr(self.secrets, name, CORE_API_DIR / path)
        return self


def load(env: str | None = None) -> Settings:
    env = env or os.environ.get("APP_ENV", "")
    if env not in ENVIRONMENTS:
        raise RuntimeError(f"APP_ENV must be one of {ENVIRONMENTS}, got {env!r}")
    return Settings(env=env)


settings = load()
