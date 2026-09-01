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

# --- Zitadel --------------------------------------------------------------
# Token verification (JWKS/discovery) reaches Zitadel over the compose
# network name.
ZITADEL_ISSUER = os.environ.get("ZITADEL_ISSUER", "http://zitadel:8080")
# The embedded login flow (app/modules/auth) historically defaulted to the
# host-resolvable name instead, because the issuer string it produces has
# to match what browsers/tokens see. Same variable, different fallback —
# docker-compose sets ZITADEL_ISSUER explicitly, so the two only diverge
# when core-api runs with no environment at all.
ZITADEL_LOGIN_ISSUER = os.environ.get("ZITADEL_ISSUER", "http://zitadel.monorepo.orb.local:8080")

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
