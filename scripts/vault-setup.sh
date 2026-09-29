#!/bin/sh
# One-time dev Vault configuration: static secrets in KV, dynamic Postgres
# credentials via the database secrets engine. Mounted into the vault
# container at /vault-setup.sh — run after `docker compose up` once vault +
# postgres are healthy:
#
#   docker compose exec vault sh /vault-setup.sh
set -eu

# --- app-secret: per-app secrets, mirrors the k8s cluster's mount name.
# core-api's login-client read (app/modules/user/auth/routes.py) is hardcoded
# to this mount, not the "secret/" default — see zitadel-bootstrap.sh /
# zitadel-oidc-restore.sh, which write the OIDC client creds here.
vault secrets enable -path=app-secret kv-v2 2>/dev/null || true

# --- static secrets (KV v2, mounted at secret/ by default in -dev mode) ---
vault kv put secret/platform/minio \
  access_key="AKIAIOSFODNN7EXAMPLE" \
  secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

vault kv put secret/platform/lakefs \
  endpoint="http://lakefs:8000" \
  access_key="AKIAIOSFODNN7EXAMPLE" \
  secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

vault kv put secret/platform/zitadel \
  masterkey="MasterkeyNeedsToHave32Characters"

# --- dynamic Postgres credentials (database secrets engine) ---
vault secrets enable -path=database database 2>/dev/null || true

vault write database/config/core-postgres \
  plugin_name=postgresql-database-plugin \
  allowed_roles="core-app" \
  connection_url="postgresql://{{username}}:{{password}}@postgres:5432/core?sslmode=disable" \
  username="postgres" \
  password="postgres"

vault write database/roles/core-app \
  db_name=core-postgres \
  creation_statements="CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}' IN ROLE core_app;" \
  revocation_statements="DROP ROLE IF EXISTS \"{{name}}\";" \
  default_ttl="1h" \
  max_ttl="24h"

echo "vault configured: secret/platform/{minio,lakefs,zitadel}, database/creds/core-app"
