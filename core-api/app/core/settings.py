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

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

CORE_API_DIR = Path(__file__).resolve().parents[2]
CONFIG_DIR = CORE_API_DIR / "config"
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


class Mlflow(_Section):
    url: str


class Observability(_Section):
    otlp_endpoint: str
    metrics_otlp_endpoint: str  # OTLP gRPC receiver for pushed metrics (core/metrics.py)
    ksm_endpoint: str


class Public(_Section):
    lakefs_endpoint: str
    labelstudio_endpoint: str


class SecretPaths(_Section):
    db: Path  # {"username", "password"} for Postgres; the Vault agent rewrites it on rotation
    lakefs: Path
    labelstudio: Path
    zitadel: Path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CORE_", env_nested_delimiter="__", extra="forbid")

    env: str
    pg: Pg
    zitadel: Zitadel
    auth: Auth
    ray: Ray
    mlflow: Mlflow
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
