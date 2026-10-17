#!/bin/sh
# Static local dev secrets -> .local/secrets/ (gitignored). Idempotent; safe to re-run.
#
# The generated ones (labelstudio.json, zitadel-oidc.json) come from steps/labelstudio.sh and
# steps/zitadel-oidc-restore.sh. Compose mounts this directory at /var/app-secret/app-backend, the path
# the cluster's Vault Agent Injector writes to, so core-api reads the same files in both places.
set -eu
. "$(dirname "$0")/lib/secrets.sh"

# Must match docker-compose.yml's minio / lakefs service environment (lakeFS reads and writes MinIO).
KEY="AKIAIOSFODNN7EXAMPLE"
SECRET="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

write_secret lakefs.json endpoint "http://lakefs:8000" access_key "$KEY" secret_key "$SECRET"

echo "secrets: $SECRETS_DIR (lakefs)"
