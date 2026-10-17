---
name: run-local
description: Set up, run, test or debug core-api locally. Use when asked to start the backend, set up a fresh checkout, run tests, bring up the docker compose stack, or when a local dependency (Postgres, Vault, lakeFS, Zitadel, MinIO, Label Studio) looks broken.
---

# Run core-api locally

Config and secrets model: skill `config-and-secrets`. This repo ships no UI; the console is the separate `customer-frontend` repo.

## Fresh checkout, full stack (in order)

```bash
mise install
./scripts/secrets-init.sh          # .local/secrets/lakefs.json
docker compose up --build -d      # fresh pgdata: compose/postgres/core-api.sql; then the dbmate service migrates before core-api starts
./scripts/zitadel-bootstrap.sh     # first time only: creates the Zitadel project and apps (re-running duplicates them)
./scripts/bootstrap.sh             # vault DB engine + .local/secrets/db.json, OIDC + Label Studio secrets, lakeFS repo; prints /health/deps
```

Native core-api (services in Docker, Python on the Mac, OrbStack required): `(cd core-api && uv sync --group dev)`, then
`docker compose up -d postgres minio lakefs zitadel ray-head lgtm mlflow labelstudio vault` (all but core-api), `./scripts/bootstrap.sh` (also writes `.local/secrets-host/`, the
copies `config/local.yaml` reads, with `<service>.<project>.orb.local` hostnames), then from `core-api/`:
`APP_ENV=local uv run uvicorn app.main:app --reload --port 8090`.

After that, every `docker compose up` is followed by `./scripts/bootstrap.sh` (idempotent). Vault is in dev mode (in memory): a restart loses the database engine; `db.json` keeps working until its 768h lease lapses, then re-run bootstrap. Static secret files live in `.local/secrets/` (gitignored) and survive.

## Everyday

| Goal | Command (from `core-api/`) |
|---|---|
| Unit tests (no services, no network) | `.venv/bin/python -m pytest tests -q` |
| API on the host, services in compose | `APP_ENV=local uv run uvicorn app.main:app --reload --port 8090` |
| Which dependency is down | `curl 127.0.0.1:8090/health/deps` |
| Migration status / roll back one (repo root) | `docker compose run --rm dbmate status` / `... rollback` |
| Migrations from an empty PG18 as backend_admin, with rollback and re-up (repo root, Docker only) | `./scripts/migrations-check.sh` |
| Lint migrations for destructive SQL (repo root) | `python3 scripts/lint-migrations.py core-api/migrations/*.sql` |

- `APP_ENV` is required: `local` (host process), `compose` (set by docker-compose.yml), `test` (set by `tests/conftest.py`).
- The `secrets` entry in `/health/deps` names a missing file or key. `postgres: error` with an auth failure means `db.json` is missing or expired: run `./scripts/bootstrap.sh`.
- Host-run (`APP_ENV=local`) reads `.local/secrets-host/`, where compose hostnames are rewritten to OrbStack names that resolve on the Mac and inside containers (Ray jobs get the lakeFS endpoint too). Without OrbStack, run core-api in compose.
- Use `127.0.0.1`, not `localhost`, from the Mac: `localhost` tries `::1` first and OrbStack resets IPv6 for containers listening on IPv4 only (Ray, MLflow, Label Studio).
- `uv run uvicorn` failing with "Failed to spawn" or a `bad interpreter` path under another directory: the venv was made at an old checkout path. `uv sync --group dev --reinstall`.
- `uv run pytest` may not resolve under the rtk shim; use `.venv/bin/python -m pytest`.
- `core-api` not starting and the `dbmate` service failing (`docker compose logs dbmate`) on a volume from before
  dbmate: it has no `backend_admin` and hand-applied tables. The fix recreates only the `core` database (README
  "Migrations"); ask before running it.
- Do not run `docker compose down -v` without asking: Postgres and Zitadel state live in those volumes.

## Ports

core-api 8090, Postgres 5432, MinIO 9000/9001, lakeFS 8000, Zitadel 8080, MLflow 5001, Ray 8265, Grafana 3000, Vault 8200, Label Studio 8110.
