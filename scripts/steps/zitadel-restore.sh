#!/bin/sh
# Restore secret/platform/zitadel-spa (Vault) and print the project id core-api checks as `aud`.
#
# A wiped dev Vault loses the SPA's client config, while Zitadel's own database keeps the project,
# the apps and the users. Re-running scripts/zitadel-bootstrap.sh would create a *second* project,
# apps and IdPs; this script instead reads the existing ids back out.
#
#   ./scripts/steps/zitadel-restore.sh
set -eu
ZITADEL="${ZITADEL_URL:-http://localhost:8080}"
HOST="Host: ${ZITADEL_HOST:-zitadel.monorepo.orb.local:8080}"
PROJECT_NAME="${ZITADEL_PROJECT:-platform}"
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
PAT=$(docker compose run --rm --no-deps -q zitadel-pat-init sh -c 'cat /pat/zitadel-automation.pat' | tr -d '\r\n')
[ -n "$PAT" ] || { echo "could not read the Zitadel PAT from the zitadel-pat volume" >&2; exit 1; }
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

SPA_CLIENT_ID=$(echo "$APPS" | "$PY" -c "
import sys, json
for a in json.load(sys.stdin).get('result') or []:
    if a['name'] == '$SPA_NAME':
        print((a.get('oidcConfig') or {}).get('clientId', '')); break
")

vault_put() {
  docker compose exec -T -e VAULT_ADDR=http://127.0.0.1:8200 -e VAULT_TOKEN=root vault \
    vault kv put "$@" > /dev/null
}

if [ -n "$SPA_CLIENT_ID" ]; then
  vault_put secret/platform/zitadel-spa \
    client_id="$SPA_CLIENT_ID" issuer="$SPA_ISSUER" redirect_uri="$SPA_REDIRECT"
  echo "vault configured: secret/platform/zitadel-spa (client_id=$SPA_CLIENT_ID)"
else
  echo "warning: app '$SPA_NAME' not found — skipped secret/platform/zitadel-spa" >&2
fi

# core-api run natively (APP_ENV=local) reads this gitignored file, so no `export` is needed. The id belongs to this stack's Zitadel
# volume, which is why it is not in the committed config/local.yaml.
OVERRIDE="$(dirname "$0")/../../core-api/config/local.override.yaml"
printf '# Written by scripts/steps/zitadel-restore.sh (gitignored): this stack'"'"'s Zitadel project id, the aud core-api checks.\nzitadel:\n  project_id: "%s"\n' "$PROJECT_ID" > "$OVERRIDE"
echo "wrote core-api/config/local.override.yaml (project_id=$PROJECT_ID); restart a native core-api to pick it up"
echo "in compose or elsewhere use: export CORE_ZITADEL__PROJECT_ID=$PROJECT_ID"
