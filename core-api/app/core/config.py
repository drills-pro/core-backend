"""Every environment knob core-api reads, in one place.

Modules import the constant they need from here instead of calling
os.environ themselves, so `grep` on this file is the complete answer to
"what can be configured?" (docker-compose.yml's core-api `environment:`
block is the other half — the two should stay in sync).

Values are read once at import time, exactly as the pre-modular code did:
nothing here re-reads the environment later.
"""
import os

# --- Postgres -------------------------------------------------------------
# The app's own connection is Vault-issued and rotated (see app/core/db.py);
# only host/port/database come from the environment.
PG_HOST = os.environ.get("PG_HOST", "postgres")
PG_PORT = os.environ.get("PG_PORT", "5432")
PG_DATABASE = os.environ.get("PG_DATABASE", "core")

# Static superuser DSN used *only* by the /health/deps liveness ping — not
# on the app's data path, see docker-compose.yml's comment on DATABASE_URL.
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://core:core@postgres:5432/core")

# --- Vault ----------------------------------------------------------------
VAULT_ADDR = os.environ.get("VAULT_ADDR", "http://vault:8200")
VAULT_TOKEN = os.environ.get("VAULT_TOKEN", "root")
# KV v2 mount for vault.get_kv() reads (platform/lakefs, platform/minio,
# platform/zitadel-oidc, ...). Vault's default mount name is "secret";
# the cluster's engine was renamed to "platform-secret" (k8s repo's
# bootstrap/03-vault-init.sh) — local dev's -dev-mode Vault still uses
# the default, so this only needs setting in the cluster.
VAULT_KV_MOUNT = os.environ.get("VAULT_KV_MOUNT", "secret")

# --- Zitadel --------------------------------------------------------------
# This app authenticates to Zitadel Cloud as a **Service User** (Users ->
# Service Users, not a Project Application) via a Zitadel-issued key: RFC
# 7523 JWT Profile, a self-signed assertion, not a shared secret. Zitadel's
# service-account key JSON is {type, keyId, key, expirationDate, userId} —
# no client_id involved; userId doubles as the assertion's iss/sub (see
# app/integrations/zitadel.py). The scalar fields are rendered to disk by
# the cluster's Vault Agent Injector sidecar (policy-scoped per service
# account, live-reload without a pod restart); the RSA private key gets its
# own file since it's multi-line and awkward in a KEY=value line format.
# Local dev's self-hosted Zitadel app predates this and is a plain PAT file
# instead — when these files are absent, ZITADEL_USER_ID/KEY_ID/PRIVATE_KEY
# fall back to "" and app/integrations/zitadel.py falls back in turn to
# that PAT file.
_ZITADEL_CREDS_PATH = os.environ.get("ZITADEL_CREDS_PATH", "/var/app-secret/zitadel.env")
_ZITADEL_KEY_PATH = os.environ.get("ZITADEL_KEY_PATH", "/var/app-secret/zitadel_key.pem")


def _read_env_file(path: str) -> dict[str, str]:
    try:
        with open(path) as f:
            return dict(line.strip().split("=", 1) for line in f if "=" in line)
    except FileNotFoundError:
        return {}


def _read_file(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return ""


_zitadel_creds = _read_env_file(_ZITADEL_CREDS_PATH)

# Token verification (JWKS/discovery) reaches Zitadel over the compose
# network name.
ZITADEL_ISSUER = _zitadel_creds.get("ISSUER_URL") or os.environ.get("ZITADEL_ISSUER", "http://zitadel:8080")
# The embedded login flow (app/modules/auth) historically defaulted to the
# host-resolvable name instead, because the issuer string it produces has
# to match what browsers/tokens see. Same variable, different fallback —
# docker-compose sets ZITADEL_ISSUER explicitly, so the two only diverge
# when core-api runs with no environment at all.
ZITADEL_LOGIN_ISSUER = _zitadel_creds.get("ISSUER_URL") or os.environ.get("ZITADEL_ISSUER", "http://zitadel.monorepo.orb.local:8080")
ZITADEL_USER_ID = _zitadel_creds.get("USER_ID", "")
ZITADEL_KEY_ID = _zitadel_creds.get("KEY_ID", "")
ZITADEL_PRIVATE_KEY = _read_file(_ZITADEL_KEY_PATH)

# --- Ray ------------------------------------------------------------------
RAY_DASHBOARD = os.environ.get("RAY_DASHBOARD", "http://ray-head:8265")
MAX_CONCURRENT_TRAINING_RUNS = int(os.environ.get("MAX_CONCURRENT_TRAINING_RUNS", "3"))

# --- MLflow ---------------------------------------------------------------
MLFLOW_URL = os.environ.get("MLFLOW_URL", "http://mlflow:5000")

# --- Observability --------------------------------------------------------
OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://lgtm:4317")
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://lgtm:3000")
# kube-state-metrics, scraped for the GPU-quota signal (req/08 FR2).
KSM_ENDPOINT = os.environ.get("KSM_ENDPOINT", "http://172.19.0.3:32581/metrics")

# --- Browser-facing endpoints ---------------------------------------------
# Presigned URLs and embed links have to be reachable from the user's
# browser, not just from inside the compose network.
LAKEFS_PUBLIC_ENDPOINT = os.environ.get("LAKEFS_PUBLIC_ENDPOINT", "http://localhost:8000")
MINIO_PUBLIC_ENDPOINT = os.environ.get("MINIO_PUBLIC_ENDPOINT", "http://localhost:9000")
LABELSTUDIO_PUBLIC_ENDPOINT = os.environ.get(
    "LABELSTUDIO_PUBLIC_ENDPOINT", "http://labelstudio.monorepo.orb.local:8080"
)
