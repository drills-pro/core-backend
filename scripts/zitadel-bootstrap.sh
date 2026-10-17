#!/bin/sh
# Real, automated Zitadel OIDC client registration (req/02) — no console
# clicks. Uses the machine PAT Zitadel writes on first init (see
# ZITADEL_FIRSTINSTANCE_* / compose/zitadel/firstinstance.yaml in
# docker-compose.yml) to drive the Management API directly.
#
# Run once after `docker compose up` (zitadel must have finished its
# first-instance setup — check `docker logs <zitadel-container> | grep
# "server is listening"` first):
#
#   ./scripts/zitadel-bootstrap.sh
set -eu

# Read from the zitadel-pat volume through its one-off init service, so this works whether core-api
# runs in compose or natively on the host.
# The previous form named the volume literally (monorepo_zitadel-pat) and
# so only worked while the checkout directory was called "monorepo" —
# compose derives the prefix from the directory name.
# `sh -c` wrapper rather than a bare `cat /pat/...`: Git Bash rewrites a
# leading-slash argument into a Windows path (C:/Program Files/Git/pat/...).
PAT=$(docker compose run --rm --no-deps -q zitadel-pat-init sh -c 'cat /pat/zitadel-automation.pat' | tr -d '\r')

ZITADEL="http://localhost:8080"
AUTH="Authorization: Bearer $PAT"
HOST="Host: zitadel.monorepo.orb.local:8080"  # externaldomain-based routing, see README

# Zitadel v4 turns on LoginV2 for every new instance, but the v2 login UI is a
# *separate* service (ghcr.io/zitadel/zitadel-login) that this compose stack
# doesn't run — the console then sends the browser to /ui/v2/login/login and
# gets {"code":5,"message":"Not Found"}. Fall back to the v1 login UI, which
# is built into the core binary. Drop this once a login service is added to
# docker-compose.yml; login v1 is deprecated and goes away in Zitadel v5.
curl -s -4 -o /dev/null "$ZITADEL/v2/features/instance" -X PUT -H "$AUTH" -H "$HOST" -H "Content-Type: application/json" -d '{"loginV2":{"required":false}}'
echo "loginV2 requirement disabled (using built-in login v1)"

PROJECT=$(curl -s -4 "$ZITADEL/management/v1/projects" -X POST -H "$AUTH" -H "$HOST" -H "Content-Type: application/json" -d '{"name":"platform"}')
PROJECT_ID=$(echo "$PROJECT" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "project: $PROJECT_ID"

# req/02 FR3: minimum role set, provisioned as project roles. platform_admin
# is the one above them: it belongs to whoever runs the platform, not to a
# tenant, and gates the cross-tenant /v1/admin routes (app/modules/admin).
for role in org_owner org_admin project_editor project_viewer platform_admin; do
  curl -s -4 "$ZITADEL/management/v1/projects/$PROJECT_ID/roles" -X POST -H "$AUTH" -H "$HOST" -H "Content-Type: application/json" \
    -d "{\"roleKey\":\"$role\",\"displayName\":\"$role\",\"group\":\"platform\"}" > /dev/null
done
echo "roles: org_owner org_admin project_editor project_viewer platform_admin"

echo "run core-api on the host with: export CORE_ZITADEL__PROJECT_ID=$PROJECT_ID"

# Public SPA client (PKCE, no secret) for frontend/ — separate app since a
# browser app can't hold a client_secret safely. appType WEB (not
# USER_AGENT) is required for a plain-http localhost redirect URI to be
# accepted — Zitadel only allows unencrypted redirects for "web" apps.
SPA=$(curl -s -4 "$ZITADEL/management/v1/projects/$PROJECT_ID/apps/oidc" -X POST -H "$AUTH" -H "$HOST" -H "Content-Type: application/json" -d '{
  "name": "core-frontend-spa",
  "redirectUris": ["http://localhost:5173/callback"],
  "postLogoutRedirectUris": ["http://localhost:5173/"],
  "responseTypes": ["OIDC_RESPONSE_TYPE_CODE"],
  "grantTypes": ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"],
  "appType": "OIDC_APP_TYPE_WEB",
  "authMethodType": "OIDC_AUTH_METHOD_TYPE_NONE"
}')
SPA_CLIENT_ID=$(echo "$SPA" | python3 -c "import sys,json;print(json.load(sys.stdin)['clientId'])")
docker compose exec -e VAULT_ADDR=http://127.0.0.1:8200 -e VAULT_TOKEN=root vault vault kv put secret/platform/zitadel-spa \
  client_id="$SPA_CLIENT_ID" issuer="http://zitadel.monorepo.orb.local:8080" redirect_uri="http://localhost:5173/callback"
echo "spa app: client_id=$SPA_CLIENT_ID"

# Seed the tenant row this org maps to — req/01/02: every Zitadel org is a
# platform tenant. Without this, real logins get 403 "organization not
# provisioned" (see app/tenancy.py).
# Addressed by service name, not the literal container name: the
# monorepo-postgres-1 form only resolved while the checkout directory
# was called "monorepo".
ORG_ID=$(curl -s -4 "$ZITADEL/management/v1/orgs/me" -H "$AUTH" -H "$HOST" | python3 -c "import sys,json;print(json.load(sys.stdin)['org']['id'])")
docker compose exec -T postgres psql -U postgres -d core -c "
INSERT INTO tenants (zitadel_org_id, name) VALUES ('$ORG_ID', 'ZITADEL (real)')
ON CONFLICT (zitadel_org_id) DO NOTHING;"
echo "tenant seeded for org $ORG_ID"

# FR1 (improvement/phase2/02-org-team-project-model.md): every org has >=1
# team by default, created at the same provisioning step as the tenant row.
docker compose exec -T postgres psql -U postgres -d core -c "
INSERT INTO teams (tenant_id, name)
SELECT id, 'Default' FROM tenants WHERE zitadel_org_id = '$ORG_ID'
AND NOT EXISTS (SELECT 1 FROM teams WHERE teams.tenant_id = tenants.id);"
echo "default team seeded for org $ORG_ID"

# FR1 (improvement/phase2/07-identity-oauth-linking.md): register Google/
# GitHub as generic OIDC IdPs on the org with account-linking allowed, so
# a login via either that presents an already-verified email links to the
# existing Zitadel user instead of creating a second one — this is what
# makes "same email, one account" happen, no app code involved.
# ponytail: client_id/client_secret below are placeholders — swap for real
# OAuth app credentials from Google/GitHub when available, nothing else
# in this script changes.
curl -s -4 "$ZITADEL/management/v1/idps/generic_oidc" -X POST -H "$AUTH" -H "$HOST" -H "Content-Type: application/json" -d '{
  "name": "google",
  "issuer": "https://accounts.google.com",
  "clientId": "REPLACE_WITH_GOOGLE_CLIENT_ID",
  "clientSecret": "REPLACE_WITH_GOOGLE_CLIENT_SECRET",
  "scopes": ["openid", "profile", "email"],
  "isLinkingAllowed": true,
  "isCreationAllowed": true,
  "isAutoCreation": false,
  "isAutoUpdate": true
}' > /dev/null
curl -s -4 "$ZITADEL/management/v1/idps/generic_oidc" -X POST -H "$AUTH" -H "$HOST" -H "Content-Type: application/json" -d '{
  "name": "github",
  "issuer": "https://github.com",
  "clientId": "REPLACE_WITH_GITHUB_CLIENT_ID",
  "clientSecret": "REPLACE_WITH_GITHUB_CLIENT_SECRET",
  "scopes": ["openid", "profile", "email"],
  "isLinkingAllowed": true,
  "isCreationAllowed": true,
  "isAutoCreation": false,
  "isAutoUpdate": true
}' > /dev/null
echo "google/github idps registered (placeholder credentials — replace before enabling in prod)"

echo "done"
