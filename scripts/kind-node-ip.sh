#!/bin/sh
# Resolve the kind cluster's node IP and pin it into .env for docker compose.
#
# Why this exists: zitadel reaches its CloudNativePG Postgres, and core-api
# reaches kube-state-metrics, over NodePort at a kind *node* IP. That address
# is assigned by Docker and is not stable — while the cluster is down the
# address is free, and the next container to join the `kind` network takes
# it. That is exactly what happened on 2026-08-25: core-api claimed
# 172.19.0.3, and zitadel then failed to start because it was talking to
# core-api instead of Postgres.
#
# Run before `docker compose up` whenever the kind cluster has been
# recreated or restarted:
#
#   ./scripts/kind-node-ip.sh && docker compose up -d
#
# docker-compose.yml reads $KIND_NODE_IP with a fallback, so a checkout with
# no .env still works — it just isn't protected against the drift above.
set -eu

CLUSTER="${KIND_CLUSTER:-platform}"
NODE="${KIND_NODE:-$CLUSTER-control-plane}"
NETWORK="${KIND_NETWORK:-kind}"
ENV_FILE="${ENV_FILE:-.env}"

docker inspect "$NODE" > /dev/null 2>&1 || {
  echo "kind node '$NODE' not found — is the cluster created and started?" >&2
  echo "  kind create cluster --name $CLUSTER --config k8s/kind-cluster.yaml" >&2
  echo "  docker start $NODE" >&2
  exit 1
}

RUNNING=$(docker inspect -f '{{.State.Running}}' "$NODE")
[ "$RUNNING" = "true" ] || { echo "kind node '$NODE' exists but is not running: docker start $NODE" >&2; exit 1; }

IP=$(docker inspect -f "{{(index .NetworkSettings.Networks \"$NETWORK\").IPAddress}}" "$NODE")
[ -n "$IP" ] || { echo "'$NODE' has no address on the '$NETWORK' network" >&2; exit 1; }

touch "$ENV_FILE"
if grep -q '^KIND_NODE_IP=' "$ENV_FILE"; then
  # sed -i differs between GNU and BSD; rewrite through a temp file instead.
  TMP=$(mktemp)
  sed "s|^KIND_NODE_IP=.*|KIND_NODE_IP=$IP|" "$ENV_FILE" > "$TMP" && mv "$TMP" "$ENV_FILE"
else
  echo "KIND_NODE_IP=$IP" >> "$ENV_FILE"
fi

echo "KIND_NODE_IP=$IP  ($NODE on the '$NETWORK' network)"
