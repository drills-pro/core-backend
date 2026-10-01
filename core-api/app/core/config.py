"""Every environment knob core-api reads, in one place.

Modules import the constant they need from here instead of calling
os.environ themselves, so `grep` on this file is the complete answer to
"what can be configured?" (docker-compose.yml's core-api `environment:`
block is the other half — the two should stay in sync).

Values are read once at import time, exactly as the pre-modular code did:
nothing here re-reads the environment later.
"""
import json
import os

# --- Postgres -------------------------------------------------------------
# The app's own connection is Vault-issued and rotated (see app/core/db.py);
# only host/port/database come from the environment.
PG_HOST = os.environ.get("PG_HOST", "postgres")
PG_PORT = os.environ.get("PG_PORT", "5432")
PG_DATABASE = os.environ.get("PG_DATABASE", "core")

# --- Vault ----------------------------------------------------------------
VAULT_ADDR = os.environ.get("VAULT_ADDR", "http://vault:8200")
VAULT_TOKEN = os.environ.get("VAULT_TOKEN", "root")

# --- Zitadel --------------------------------------------------------------
# This app authenticates to Zitadel Cloud as a **Service User** (Users ->
# Service Users, not a Project Application) via a Zitadel-issued key: RFC
# 7523 JWT Profile, a self-signed assertion, not a shared secret. Zitadel's
# service-account key JSON is {type, keyId, key, expirationDate, userId} —
# no client_id involved; userId doubles as the assertion's iss/sub (see
# app/integrations/zitadel.py). Rendered by the cluster's Vault Agent Injector sidecar to
# /var/app-secret/app-backend/zitadel as that secret's data verbatim, JSON-encoded — see
# k8s/apps/backend/deployment.yaml. JSON handles the RSA private key's embedded newlines fine,
# so it doesn't need its own file the way a KEY=value line would have.
# Local dev's self-hosted Zitadel app predates this and is a plain PAT file instead — when this
# file is absent, ZITADEL_USER_ID/KEY_ID/PRIVATE_KEY fall back to "" and
# app/integrations/zitadel.py falls back in turn to that PAT file.
_ZITADEL_CREDS_PATH = os.environ.get("ZITADEL_CREDS_PATH", "/var/app-secret/app-backend/zitadel")


def _read_json_file(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


_zitadel_creds = _read_json_file(_ZITADEL_CREDS_PATH)

# Token verification (JWKS/discovery) reaches Zitadel over the compose
# network name. Trailing slash stripped: Zitadel Cloud's console hands the
# issuer out as "https://<instance>.zitadel.cloud/", but the `iss` it signs
# into tokens has none, and paths are joined onto this with f"{...}/...".
ZITADEL_ISSUER = (_zitadel_creds.get("ISSUER_URL") or os.environ.get("ZITADEL_ISSUER", "http://zitadel:8080")).rstrip("/")
# The embedded login flow (app/modules/auth) historically defaulted to the
# host-resolvable name instead, because the issuer string it produces has
# to match what browsers/tokens see. Same variable, different fallback —
# docker-compose sets ZITADEL_ISSUER explicitly, so the two only diverge
# when core-api runs with no environment at all.
ZITADEL_LOGIN_ISSUER = (_zitadel_creds.get("ISSUER_URL") or os.environ.get("ZITADEL_ISSUER", "http://zitadel.monorepo.orb.local:8080")).rstrip("/")
ZITADEL_USER_ID = _zitadel_creds.get("USER_ID", "")
ZITADEL_KEY_ID = _zitadel_creds.get("KEY_ID", "")
ZITADEL_PRIVATE_KEY = _zitadel_creds.get("PRIVATE_KEY", "")

# The admin console's sign-in (app/modules/admin/auth): a confidential OIDC
# Web app in Zitadel, authorization code + PKCE, client_secret_basic. A
# third credential, unrelated to the Service User above and to the user
# login's own client — this one only ever signs in platform operators.
ZITADEL_ADMIN_CLIENT_ID = os.environ.get("ZITADEL_ADMIN_CLIENT_ID", "")
ZITADEL_ADMIN_CLIENT_SECRET = os.environ.get("ZITADEL_ADMIN_CLIENT_SECRET", "")
# Zitadel sends the browser back to core-api's own /callback, as the
# browser reaches it (the host port, not the compose service name); after
# logout it goes to the console's sign-in page. Each must match the Zitadel
# app's registered list exactly.
ZITADEL_ADMIN_REDIRECT_URI = os.environ.get(
    "ZITADEL_ADMIN_REDIRECT_URI", "http://localhost:8090/v1/admin/auth/callback"
)
ZITADEL_ADMIN_POST_LOGOUT_REDIRECT_URI = os.environ.get(
    "ZITADEL_ADMIN_POST_LOGOUT_REDIRECT_URI", "http://localhost:3001/login"
)
# Where /callback hands the browser back to: the console's origin, as the
# browser reaches it.
ADMIN_CONSOLE_URL = os.environ.get("ADMIN_CONSOLE_URL", "http://localhost:3001").rstrip("/")

# --- Object storage --------------------------------------------------------
# Backend's own gs://drills-pro bucket, accessed via GCS's S3-compatible interoperability
# API (not the AWS S3 lakefs/mlflow use) — app-secret/app-backend/s3, rendered the same way
# as the Zitadel creds above, to /var/app-secret/app-backend/s3.
_S3_CREDS_PATH = os.environ.get("S3_CREDS_PATH", "/var/app-secret/app-backend/s3")
_s3_creds = _read_json_file(_S3_CREDS_PATH)
AWS_ACCESS_KEY_ID = _s3_creds.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = _s3_creds.get("AWS_SECRET_ACCESS_KEY", "")
S3_ENDPOINT = _s3_creds.get("S3_ENDPOINT", "")
S3_BUCKET = _s3_creds.get("S3_BUCKET", "")

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
