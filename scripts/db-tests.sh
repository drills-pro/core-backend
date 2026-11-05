#!/bin/sh
# Run the database-backed tests (core-api/tests/db) on a real PostgreSQL 18: the compose postgres in a throwaway
# project, migrated by dbmate the way the cluster's backend-migrate Job does it, then pytest as backend_admin (the
# owner; the tests switch to core_app themselves). Needs Docker and `uv sync --group dev` in core-api.
#
#   ./scripts/db-tests.sh [pytest args]
set -eu
cd "$(dirname "$0")/.."

# A project and a free host port of its own, so it runs next to a live dev stack.
work=$(mktemp -d)
export COMPOSE_PROJECT_NAME=db-tests-$$
printf 'services:\n  postgres:\n    ports: !override ["127.0.0.1::5432"]\n' > "$work/ports.yml"
dc() { docker compose -f docker-compose.yml -f "$work/ports.yml" "$@"; }
trap 'dc down -v >/dev/null 2>&1 || true; rm -rf "$work"' EXIT

dc up -d --wait postgres
dc run --rm -q dbmate up
port=$(dc port postgres 5432 | sed 's/.*://')

cd core-api
# backend_admin's password is the laptop-only value in compose/postgres/core-api.sql.
ISOLATION_DB_DSN="postgresql://backend_admin:backend_admin@127.0.0.1:$port/core" .venv/bin/python -m pytest tests/db -q "$@"
