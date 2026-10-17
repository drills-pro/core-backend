#!/bin/sh
# Static local dev secrets -> .local/secrets/ (gitignored). Idempotent; safe to re-run.
#
# The generated ones (labelstudio.json, zitadel-oidc.json) come from labelstudio-bootstrap.sh and
# zitadel-oidc-restore.sh. Compose mounts this directory at /var/app-secret/app-backend, the path
# the cluster's Vault Agent Injector writes to, so core-api reads the same files in both places.
set -eu
. "$(dirname "$0")/lib-secrets.sh"

# Must match docker-compose.yml's minio / lakefs service environment.
KEY="AKIAIOSFODNN7EXAMPLE"
SECRET="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

write_secret minio.json access_key "$KEY" secret_key "$SECRET"
write_secret lakefs.json endpoint "http://lakefs:8000" access_key "$KEY" secret_key "$SECRET"
mkdir -p "$SECRETS_DIR"
printf 'root' > "$SECRETS_DIR/vault-token"   # the dev Vault's root token (docker-compose.yml)
chmod 644 "$SECRETS_DIR/vault-token"

echo "secrets: $SECRETS_DIR (minio, lakefs, vault-token)"
