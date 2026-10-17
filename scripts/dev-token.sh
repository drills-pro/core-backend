#!/bin/sh
# Get a Zitadel access token for curl, with the device flow: opens the login page, waits for you to
# sign in, prints the token on stdout (everything else goes to stderr).
#
#   export TOKEN=$(./scripts/dev-token.sh)
#   curl -s $API/v1/me -H "Authorization: Bearer $TOKEN" | jq
#
# One-time setup in the Zitadel console (project `platform`): a Native app with grant type Device
# Code and Auth Token Type JWT. Put its client id in ZITADEL_CLIENT_ID (or in .local/dev-client-id).
# The token is cached in .local/dev-token (gitignored) until it expires; FORCE=1 skips the cache.
set -eu

ISSUER="${ZITADEL_ISSUER:-https://auth.drills.pro}"
PROJECT_ID="${ZITADEL_PROJECT_ID:-395337035725930916}"
cd "$(dirname "$0")/.."
mkdir -p .local
CLIENT_ID="${ZITADEL_CLIENT_ID:-$(cat .local/dev-client-id 2>/dev/null || true)}"
[ -n "$CLIENT_ID" ] || { echo "set ZITADEL_CLIENT_ID (the Native device-code app's client id)" >&2; exit 1; }
printf '%s\n' "$CLIENT_ID" > .local/dev-client-id

# Cached and still valid for another minute? Reuse it.
if [ -z "${FORCE:-}" ] && [ -s .local/dev-token ]; then
  exp=$(jq -R 'split(".")[1] | gsub("-";"+") | gsub("_";"/") | @base64d | fromjson | .exp' .local/dev-token 2>/dev/null || echo 0)
  if [ "$exp" -gt "$(( $(date +%s) + 60 ))" ] 2>/dev/null; then cat .local/dev-token; exit 0; fi
fi

dev=$(curl -sf "$ISSUER/oauth/v2/device_authorization" -d "client_id=$CLIENT_ID" \
  --data-urlencode "scope=openid email profile urn:zitadel:iam:org:project:id:$PROJECT_ID:aud") \
  || { echo "device authorization failed: is the app a Native app with Device Code enabled?" >&2; exit 1; }
code=$(echo "$dev" | jq -r .device_code)
url=$(echo "$dev" | jq -r .verification_uri_complete)
wait=$(echo "$dev" | jq -r '.interval // 5')

echo "Sign in at: $url" >&2
echo "(code: $(echo "$dev" | jq -r .user_code))" >&2
open "$url" 2>/dev/null || xdg-open "$url" 2>/dev/null || true

while :; do
  sleep "$wait"
  res=$(curl -s "$ISSUER/oauth/v2/token" -d grant_type=urn:ietf:params:oauth:grant-type:device_code \
    -d "device_code=$code" -d "client_id=$CLIENT_ID")
  case "$(echo "$res" | jq -r '.error // "ok"')" in
    ok) break ;;
    authorization_pending) ;;
    slow_down) wait=$((wait + 5)) ;;
    *) echo "login failed: $(echo "$res" | jq -r '.error_description // .error')" >&2; exit 1 ;;
  esac
done

token=$(echo "$res" | jq -r .access_token)
# A JWT has two dots; Zitadel's default encrypted token has four.
[ "$(printf %s "$token" | tr -cd . | wc -c)" -eq 2 ] \
  || { echo "got an opaque token: set the app's Auth Token Type to JWT in the Zitadel console" >&2; exit 1; }
printf '%s\n' "$token" > .local/dev-token
chmod 600 .local/dev-token
printf '%s\n' "$token"
