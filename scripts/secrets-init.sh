#!/bin/sh
# Static local dev secrets -> .local/secrets/ (gitignored). Idempotent; safe to re-run.
#
# Compose mounts this directory at /var/app-secret/app-backend, the path
# the cluster's Vault Agent Injector writes to, so core-api reads the same files in both places.
set -eu
. "$(dirname "$0")/lib/secrets.sh"

# Must match docker-compose.yml's minio / lakefs service environment (lakeFS reads and writes MinIO).
KEY="AKIAIOSFODNN7EXAMPLE"
SECRET="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

write_secret lakefs.json endpoint "http://lakefs:8000" access_key "$KEY" secret_key "$SECRET"
write_secret staging.json endpoint "http://minio:9000" access_key "$KEY" secret_key "$SECRET"
# Bucket-only MinIO user created by steps/lakefs.sh: the ingest job runs hostile input and gets nothing else.
write_secret ingest.json endpoint "http://minio:9000" access_key "ingest" secret_key "ingest-local-secret"
write_secret webhooks.json signing_key "local-webhook-signing-key"

echo "secrets: $SECRETS_DIR (lakefs, staging, ingest, webhooks)"
