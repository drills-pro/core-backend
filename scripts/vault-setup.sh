#!/bin/sh
# One-time dev Vault configuration: dynamic Postgres credentials via the
# database secrets engine. Mounted into the vault
# container at /vault-setup.sh — run after `docker compose up` once vault +
# postgres are healthy:
#
#   docker compose exec vault sh /vault-setup.sh
set -eu

# Static secrets (lakefs, labelstudio, zitadel-oidc) are no longer in Vault: core-api reads
# them from JSON files (scripts/secrets-init.sh). Vault only issues the Postgres credentials below.

# The zitadel *server's* own masterkey (docker-compose.yml passes it directly as a CLI flag).
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
  default_ttl="768h" \
  max_ttl="768h"
# 768h is the dev server's maximum. Nothing renews the lease locally, so re-run bootstrap.sh after a month.

echo "vault configured: secret/platform/zitadel, database/creds/core-app"
