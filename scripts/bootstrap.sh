#!/bin/sh
# Bring an already-running stack back to a working state, in one command.
#
#   docker compose up -d && ./scripts/bootstrap.sh
#
# Why this is needed at all: Vault runs in dev mode, which keeps everything
# in memory. Restarting that one container empties it, so the database engine
# and core-api's Postgres credential (.local/secrets/db.json) are set up again. (Static secrets are files under .local/secrets,
# not in Vault, so they survive.) Every step below is
# idempotent, so running this after any `docker compose up` is safe and is
# the intended habit.
#
# NOT covered: scripts/zitadel-bootstrap.sh, which creates the Zitadel
# project, OIDC apps, roles and IdPs. That one is first-time-only and would
# create duplicates if re-run — steps/zitadel-oidc-restore.sh below is the
# re-runnable half, and it tells you if the first-time setup is missing.
set -eu

cd "$(dirname "$0")/.."

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

step "local secret files"
./scripts/secrets-init.sh

step "vault: database secrets engine, core-api's Postgres credential"
VAULT="docker compose exec -T -e VAULT_ADDR=http://127.0.0.1:8200 -e VAULT_TOKEN=root vault"
$VAULT sh -c 'sh /vault-setup.sh' | tail -1
# Same shape as the cluster's /vault/secrets/db. Each run issues a new Postgres user; old ones lapse at
# VALID UNTIL.
. ./scripts/lib/secrets.sh
creds=$($VAULT vault read -format=json database/creds/core-app)
user=$(printf '%s\n' "$creds" | sed -n 's/.*"username": "\(.*\)".*/\1/p')
pass=$(printf '%s\n' "$creds" | sed -n 's/.*"password": "\(.*\)".*/\1/p')
write_secret db.json username "$user" password "$pass"
echo "wrote $SECRETS_DIR/db.json ($user)"

step "zitadel OIDC client credentials"
./scripts/steps/zitadel-oidc-restore.sh

step "label studio refresh token"
./scripts/steps/labelstudio.sh

step "lakefs: admin, bucket, repository"
./scripts/steps/lakefs.sh

step "restarting core-api"
docker compose restart core-api > /dev/null

printf '\n'
i=0
while [ "$i" -lt 30 ]; do
  if [ "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8090/health)" = "200" ]; then
    break
  fi
  i=$((i + 1)); sleep 2
done

step "health"
curl -s http://localhost:8090/health/deps
printf '\n'
