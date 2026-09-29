#!/bin/sh
# Restore app-secret/app-backend/zitadel-oidc and secret/platform/zitadel-spa into Vault.
#
# Vault runs in dev mode (in-memory), so a vault restart loses the OIDC
# client credentials app/modules/auth/routes.py needs — while Zitadel's own
# database (CloudNativePG on the kind cluster) keeps the project, the apps
# and the users. Re-running scripts/zitadel-bootstrap.sh would create a
# *second* project, apps and IdPs; this script instead reads the existing
# ids back out and re-issues just the secret.
#
#   ./scripts/zitadel-oidc-restore.sh
#
# The client secret cannot be read back — Zitadel returns it only at
# creation — so it is regenerated. Any copy of the previous secret stops
# working, which after a Vault wipe is true regardless.
set -eu

ZITADEL="${ZITADEL_URL:-http://localhost:8080}"
HOST="Host: ${ZITADEL_HOST:-zitadel.monorepo.orb.local:8080}"
PROJECT_NAME="${ZITADEL_PROJECT:-platform}"
APP_NAME="${ZITADEL_APP:-core-api-web}"
SPA_NAME="${ZITADEL_SPA:-core-frontend-spa}"
SPA_ISSUER="${ZITADEL_SPA_ISSUER:-http://zitadel.monorepo.orb.local:8080}"
SPA_REDIRECT="${ZITADEL_SPA_REDIRECT:-http://localhost:5173/callback}"

# Windows/Git-Bash hosts ship `python` but no `python3`; Linux/macOS the
# other way round. Probe by *running* each: Windows keeps a python3.exe
# stub on PATH that `command -v` finds but that only prints a Microsoft
# Store advert when executed.
for candidate in python3 python; do
  if "$candidate" -c '' 2>/dev/null; then PY=$candidate; break; fi
done
[ -n "${PY:-}" ] || { echo "need python to parse Zitadel's JSON responses" >&2; exit 1; }

# `sh -c` wrapper rather than a bare `cat /pat/...`: Git Bash rewrites a
# leading-slash argument into a Windows path (C:/Program Files/Git/pat/...).
PAT=$(docker compose exec -T core-api sh -c 'cat /pat/zitadel-automation.pat' | tr -d '\r\n')
[ -n "$PAT" ] || { echo "could not read the Zitadel PAT from core-api" >&2; exit 1; }
AUTH="Authorization: Bearer $PAT"

PROJECT_ID=$(curl -sf -4 "$ZITADEL/management/v1/projects/_search" -X POST \
  -H "$AUTH" -H "$HOST" -H "Content-Type: application/json" -d '{}' \
  | "$PY" -c "
import sys, json
want = '$PROJECT_NAME'
for p in json.load(sys.stdin).get('result') or []:
    if p['name'] == want:
        print(p['id']); break
")
[ -n "$PROJECT_ID" ] || {
  echo "project '$PROJECT_NAME' does not exist in Zitadel." >&2
  echo "This is a first-time setup, not a restore — run ./scripts/zitadel-bootstrap.sh instead." >&2
  exit 1; }

APPS=$(curl -sf -4 "$ZITADEL/management/v1/projects/$PROJECT_ID/apps/_search" -X POST \
  -H "$AUTH" -H "$HOST" -H "Content-Type: application/json" -d '{}')

read_app() {
  echo "$APPS" | "$PY" -c "
import sys, json
want, field = '$1', '$2'
for a in json.load(sys.stdin).get('result') or []:
    if a['name'] == want:
        print(a['id'] if field == 'appId' else (a.get('oidcConfig') or {}).get('clientId', '')); break
"
}

APP_ID=$(read_app "$APP_NAME" appId)
CLIENT_ID=$(read_app "$APP_NAME" clientId)
SPA_CLIENT_ID=$(read_app "$SPA_NAME" clientId)
[ -n "$APP_ID" ] && [ -n "$CLIENT_ID" ] || { echo "app '$APP_NAME' not found in project $PROJECT_ID" >&2; exit 1; }

CLIENT_SECRET=$(curl -sf -4 \
  "$ZITADEL/management/v1/projects/$PROJECT_ID/apps/$APP_ID/oidc_config/_generate_client_secret" \
  -X POST -H "$AUTH" -H "$HOST" -H "Content-Type: application/json" -d '{}' \
  | "$PY" -c "import sys,json;print(json.load(sys.stdin).get('clientSecret',''))")
[ -n "$CLIENT_SECRET" ] || { echo "Zitadel did not return a regenerated client secret" >&2; exit 1; }

vault_put() {
  docker compose exec -T -e VAULT_ADDR=http://127.0.0.1:8200 -e VAULT_TOKEN=root vault \
    vault kv put "$@" > /dev/null
}

vault_put app-secret/app-backend/zitadel-oidc \
  client_id="$CLIENT_ID" client_secret="$CLIENT_SECRET" \
  project_id="$PROJECT_ID" app_id="$APP_ID"
echo "vault configured: app-secret/app-backend/zitadel-oidc (client_id=$CLIENT_ID)"

if [ -n "$SPA_CLIENT_ID" ]; then
  vault_put secret/platform/zitadel-spa \
    client_id="$SPA_CLIENT_ID" issuer="$SPA_ISSUER" redirect_uri="$SPA_REDIRECT"
  echo "vault configured: secret/platform/zitadel-spa (client_id=$SPA_CLIENT_ID)"
else
  echo "warning: app '$SPA_NAME' not found — skipped secret/platform/zitadel-spa" >&2
fi
