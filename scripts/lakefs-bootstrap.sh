#!/bin/sh
# One-time LakeFS setup: admin user, blockstore bucket, and the single
# repository app/lakefs_client.py addresses (REPO = "platform-datasets" —
# one branch per platform dataset lives inside it).
#
# Without the repository, every POST /v1/projects/{id}/datasets 500s in
# ensure_branch: creating a branch in a repo that doesn't exist is a 404.
# /health/deps does not catch it — its lakefs_s3 probe only exercises the
# S3 gateway, not the repo.
#
# Run once after `docker compose up` (lakefs + minio must be healthy):
#
#   ./scripts/lakefs-bootstrap.sh
#
# Idempotent, unlike scripts/zitadel-bootstrap.sh: re-running it against an
# already-provisioned stack reports what already exists and exits 0, so it
# is safe to run after every `docker compose up`.
set -eu

LAKEFS="http://localhost:8000"
# Must match app-secret/app-backend/lakefs in scripts/vault-setup.sh — core-api
# reads its LakeFS credentials from Vault, not from env vars.
ACCESS_KEY="AKIAIOSFODNN7EXAMPLE"
SECRET_KEY="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
REPO="platform-datasets"
# Own bucket rather than the `datasets` one app/object_storage.py manages:
# LakeFS wants a storage namespace it alone controls.
BUCKET="lakefs-data"

AUTH="$ACCESS_KEY:$SECRET_KEY"

# --- admin user (first boot only; 409 once an admin exists) ---
SETUP_CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$LAKEFS/api/v1/setup_lakefs" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"key\":{\"access_key_id\":\"$ACCESS_KEY\",\"secret_access_key\":\"$SECRET_KEY\"}}")
case "$SETUP_CODE" in
  200) echo "lakefs admin: created" ;;
  409) echo "lakefs admin: already set up" ;;
  *)   echo "lakefs setup_lakefs failed (HTTP $SETUP_CODE)" >&2; exit 1 ;;
esac

# --- blockstore bucket (LAKEFS_BLOCKSTORE_S3_ENDPOINT points at minio) ---
# `local` is the alias the minio image configures for its own healthcheck.
docker compose exec -T minio mc mb -p "local/$BUCKET" > /dev/null
echo "minio bucket: $BUCKET"

# --- the repository core-api expects ---
REPO_CODE=$(curl -s -o /dev/null -w '%{http_code}' -u "$AUTH" "$LAKEFS/api/v1/repositories/$REPO")
if [ "$REPO_CODE" = "200" ]; then
  echo "lakefs repo: $REPO already exists"
else
  CREATE_CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$LAKEFS/api/v1/repositories" \
    -u "$AUTH" -H 'Content-Type: application/json' \
    -d "{\"name\":\"$REPO\",\"storage_namespace\":\"s3://$BUCKET/$REPO\",\"default_branch\":\"main\"}")
  [ "$CREATE_CODE" = "201" ] || { echo "lakefs repo create failed (HTTP $CREATE_CODE)" >&2; exit 1; }
  echo "lakefs repo: $REPO created (s3://$BUCKET/$REPO)"
fi

echo "done"
