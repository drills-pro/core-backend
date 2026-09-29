#!/bin/sh
# Bring an already-running stack back to a working state, in one command.
#
#   docker compose up -d && ./scripts/bootstrap.sh
#
# Why this is needed at all: Vault runs in dev mode, which keeps everything
# in memory. Restarting that one container empties it, and core-api then
# cannot get so much as a Postgres credential. Every step below is
# idempotent, so running this after any `docker compose up` is safe and is
# the intended habit. (The kind node IP dance in scripts/kind-node-ip.sh is
# now only relevant if you're also running a kind cluster for k8s-adjacent
# testing — plain local dev no longer needs it, see docker-compose.yml.)
#
# NOT covered: scripts/zitadel-bootstrap.sh, which creates the Zitadel
# project, OIDC apps, roles and IdPs. That one is first-time-only and would
# create duplicates if re-run — zitadel-oidc-restore.sh below is the
# re-runnable half, and it tells you if the first-time setup is missing.
set -eu

cd "$(dirname "$0")/.."

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

if docker inspect "${KIND_NODE:-platform-control-plane}" > /dev/null 2>&1; then
  step "kind node IP"
  ./scripts/kind-node-ip.sh
  echo "note: if this changed and you're using KIND_NODE_IP for the GPU-quota metric, re-run 'docker compose up -d'"
else
  step "kind node IP"
  echo "no kind cluster — skipping (only the GPU-quota metric poller wants it, harmless without)"
fi

step "vault: KV + database secrets engine"
docker compose exec -T -e VAULT_ADDR=http://127.0.0.1:8200 -e VAULT_TOKEN=root vault \
  sh -c 'sh /vault-setup.sh' | tail -1

step "vault: zitadel OIDC client credentials"
./scripts/zitadel-oidc-restore.sh

step "vault: label studio refresh token"
./scripts/labelstudio-bootstrap.sh

step "lakefs: admin, bucket, repository"
./scripts/lakefs-bootstrap.sh

step "restarting core-api to pick up the restored secrets"
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
