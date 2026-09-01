#!/bin/sh
# Label Studio credential bootstrap (req/04 labeling).
#
# Label Studio 1.23 disables legacy static API tokens org-wide
# (`legacy_api_tokens_enabled: false` — a `Token <...>` header gets 401), so
# app/integrations/labelstudio_client.py authenticates with a long-lived JWT
# *refresh* token that it exchanges for short-lived access tokens. This
# script obtains that refresh token and stores it in Vault.
#
# Vault runs in dev mode (in-memory): every `docker compose restart vault`
# wipes it, so run this alongside scripts/vault-setup.sh.
#
#   ./scripts/labelstudio-bootstrap.sh
#
# Re-runnable, but not free: Label Studio allows one refresh token per user
# and refuses to mint a second (409), so step 2 revokes the existing one
# first. Any copy of the previous token stops working — which is exactly the
# situation after a Vault wipe, where the old token is unrecoverable anyway
# (GET /api/token/ returns it without its signature, deliberately).
#
# Lives outside vault-setup.sh because that script runs *inside* the Vault
# container, whose BusyBox wget cannot hold the session cookie this flow
# needs. Same host-side shape as scripts/zitadel-bootstrap.sh.
set -eu

LS="${LS_URL:-http://localhost:8110}"                      # host-side, for this script
LS_INTERNAL="${LS_INTERNAL_URL:-http://labelstudio:8080}"  # what core-api uses
LS_EMAIL="${LS_EMAIL:-admin@platform.local}"
LS_PASSWORD="${LS_PASSWORD:-LabelStudioAdminPw1!}"

JAR=$(mktemp)
trap 'rm -f "$JAR"' EXIT

# JSON parsed with cut rather than python3 so this runs anywhere docker
# compose does — Windows/Git-Bash hosts have `python` but no `python3`.
json_token() { tr ',' '\n' | grep '"token"' | cut -d'"' -f4; }

# 1. Django session login. The CSRF token is embedded in the login form and
#    must be echoed back both as a form field and via the cookie.
CSRF=$(curl -sf -c "$JAR" "$LS/user/login/" \
  | grep -o 'name="csrfmiddlewaretoken" value="[^"]*"' | head -1 | sed 's/.*value="//;s/"//')
[ -n "$CSRF" ] || { echo "could not read csrfmiddlewaretoken from $LS/user/login/" >&2; exit 1; }

STATUS=$(curl -s -b "$JAR" -c "$JAR" -o /dev/null -w '%{http_code}' -X POST "$LS/user/login/" \
  -H "Referer: $LS/user/login/" \
  --data-urlencode "csrfmiddlewaretoken=$CSRF" \
  --data-urlencode "email=$LS_EMAIL" \
  --data-urlencode "password=$LS_PASSWORD")
# A successful Django login redirects; a failed one re-renders the form as 200.
[ "$STATUS" = "302" ] || { echo "login failed for $LS_EMAIL (HTTP $STATUS)" >&2; exit 1; }

# Post-login CSRF cookie — Django rotates it on session change.
CSRF=$(grep csrftoken "$JAR" | awk '{print $NF}' | tail -1)
api() { curl -sf -b "$JAR" -H "X-CSRFToken: $CSRF" -H "Referer: $LS/" -H "Content-Type: application/json" "$@"; }

# 2. Revoke whatever token this user already holds, or step 3 gets a 409.
#    The listing omits the signature, but that truncated form is still
#    enough for the blacklist endpoint to identify and kill it.
EXISTING=$(api "$LS/api/token/" | json_token || true)
if [ -n "$EXISTING" ]; then
  api -o /dev/null -X POST "$LS/api/token/blacklist/" -d "{\"refresh\":\"$EXISTING\"}"
  echo "revoked previous refresh token"
fi

# 3. Mint the new one.
REFRESH=$(api -X POST "$LS/api/token/" -d '{}' | json_token)
[ -n "$REFRESH" ] || { echo "no refresh token returned by $LS/api/token/" >&2; exit 1; }

# 4. Prove it before storing it — a token that cannot be exchanged is worse
#    than no token, because the failure only surfaces at labeling time.
curl -sf -X POST "$LS/api/token/refresh/" -H "Content-Type: application/json" \
  -d "{\"refresh\":\"$REFRESH\"}" > /dev/null \
  || { echo "refresh token was rejected by /api/token/refresh/" >&2; exit 1; }

docker compose exec -T -e VAULT_ADDR=http://127.0.0.1:8200 -e VAULT_TOKEN=root vault \
  vault kv put secret/platform/labelstudio refresh_token="$REFRESH" url="$LS_INTERNAL" > /dev/null

echo "vault configured: secret/platform/labelstudio (url=$LS_INTERNAL)"
