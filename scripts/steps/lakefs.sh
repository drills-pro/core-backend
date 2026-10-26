#!/bin/sh
# One-time LakeFS setup: admin user, the blockstore bucket, and the staging bucket the ingest job writes to.
# Repositories are created per dataset by core-api (app/integrations/data_plane).
#
# Run once after `docker compose up` (lakefs + minio must be healthy):
#
#   ./scripts/steps/lakefs.sh
#
# Idempotent, unlike scripts/zitadel-bootstrap.sh: re-running it against an
# already-provisioned stack reports what already exists and exits 0, so it
# is safe to run after every `docker compose up`.
set -eu

LAKEFS="http://localhost:8000"
# Must match lakefs.json from scripts/secrets-init.sh — core-api reads its LakeFS
# credentials from that file, not from env vars.
ACCESS_KEY="AKIAIOSFODNN7EXAMPLE"
SECRET_KEY="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
# Own bucket rather than the `datasets` one app/object_storage.py manages:
# LakeFS wants a storage namespace it alone controls.
BUCKET="lakefs-data"

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
# The image's own `local` alias has no credentials, so set it from the container's root user first.
docker compose exec -T minio sh -c 'mc alias set local http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null && mc mb -p "local/$0" >/dev/null' "$BUCKET"
echo "minio bucket: $BUCKET"

# --- staging bucket, and an `ingest` user that can only touch it ---
# The ingest job writes under imports/<tenant>/<dataset>/<import>/ (data_plane/layout.py) in this bucket and has no lakeFS credential;
# core-api creates one lakeFS repository per dataset itself (data_plane.create_dataset_repo).
# ponytail: the policy scopes the job to the staging bucket, not to its own import prefix; prefix-only access needs
# per-job STS credentials (docs/datasets-plan.md, risks).
STAGING_BUCKET=$(sed -n 's/^ *staging_bucket: *//p' "$(dirname "$0")/../../core-api/config/base.yaml")  # data_plane.staging_bucket
[ -n "$STAGING_BUCKET" ] || { echo "data_plane.staging_bucket missing from base.yaml" >&2; exit 1; }
docker compose exec -T minio sh -c '
  mc alias set local http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null &&
  mc mb -p "local/$0" >/dev/null &&
  printf "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\",\"s3:ListBucket\"],\"Resource\":[\"arn:aws:s3:::$0\",\"arn:aws:s3:::$0/*\"]}]}" >/tmp/ingest-policy.json &&
  { mc admin policy create local ingest /tmp/ingest-policy.json >/dev/null || true; } &&
  { mc admin user add local ingest ingest-local-secret >/dev/null || true; } &&
  mc admin policy attach local ingest --user ingest >/dev/null 2>&1 || true' "$STAGING_BUCKET"
echo "minio bucket: $STAGING_BUCKET (user ingest, bucket-only policy)"

echo "done"
