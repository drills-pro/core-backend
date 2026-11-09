---
name: run-local
description: Set up, run, test or debug core-api locally, either natively on the Mac (uvicorn) against Docker services or fully in docker compose. Use when asked to start the backend, run the server, set up a fresh checkout, run tests, bring up the compose stack, or when a local dependency (Postgres, Vault, lakeFS, Zitadel, MinIO, MLflow, Ray) looks broken.
---

# Run core-api locally

Config and secrets model: skill `config-and-secrets`. This repo ships no UI; the console is the separate
`customer-frontend` repo. Commands run from the repo root unless they say `core-api/`.

Docker is OrbStack (`docker context show` → `orbstack`). `docker info` failing means it is not running:
`open -a OrbStack`. Native mode needs OrbStack; compose mode works on any Docker.

## Two ways to run

| | Native (default for development) | Compose |
|---|---|---|
| core-api | `uvicorn --reload` on the Mac, `APP_ENV=local`, `config/local.yaml` | container, `APP_ENV=compose`, `config/compose.yaml` |
| Services | compose, every service except `core-api` | compose, everything |
| Secret files | `.local/secrets-host/` (hostnames rewritten for the Mac) | `.local/secrets/`, mounted at the cluster path |
| URL | http://127.0.0.1:8090 | http://localhost:8090 |

The service list for native mode, used below as `$DEPS`:

```bash
DEPS="postgres minio lakefs zitadel ray-head lgtm mlflow vault"
```

## First time on a checkout

```bash
mise install
(cd core-api && uv sync --group dev)
./scripts/secrets-init.sh              # static secrets: .local/secrets/{lakefs,staging,ingest}.json
docker compose up -d --build $DEPS     # or without $DEPS for compose mode
./scripts/zitadel-bootstrap.sh         # ONCE per Zitadel volume: project, OIDC apps, roles (re-running duplicates them)
./scripts/bootstrap.sh
```

A fresh `pgdata` volume runs `compose/postgres/*.sql` (other services' databases; `backend_admin`, `core_app`
and the `core` database, shaped like the cluster), and the `dbmate` service applies `core-api/migrations/`
before anything that depends on it. `zitadel-bootstrap.sh` also seeds the tenant row for your Zitadel org.

## Every day

```bash
docker compose up -d $DEPS
./scripts/bootstrap.sh
cd core-api && APP_ENV=local uv run uvicorn app.main:app --reload --port 8090
```

Re-run `bootstrap.sh` after any `docker compose up`: Vault runs in dev mode (in memory), so a restart loses the
database engine and core-api's Postgres credential. Every step is idempotent. It:

1. writes static secrets (`secrets-init.sh`);
2. configures Vault's database engine and writes a fresh `db.json` (768h lease, nothing renews it locally);
3. restores the SPA client config in Vault and prints the Zitadel project id to export as
   `CORE_ZITADEL__PROJECT_ID` (`scripts/steps/zitadel-restore.sh`);
4. provisions lakeFS and MinIO (`steps/lakefs.sh`): lakeFS admin, the blockstore and staging buckets, and the
   bucket-scoped `ingest` user. core-api creates one lakeFS repository per dataset itself;
5. writes `.local/secrets-host/`: copies of `.local/secrets/*.json` with `http://<service>:<port>` rewritten to
   `http://<service>.<project>.orb.local:<port>`. Those names resolve on the Mac and inside containers, which
   matters because core-api hands the `ingest` credential and staging endpoint on to Ray ingest jobs running in `ray-head`;
6. restarts the `core-api` container if it runs in compose and prints `/health/deps`; otherwise prints the
   uvicorn command.

Done when `curl -s 127.0.0.1:8090/health/deps` shows every entry `ok`: postgres, secrets, lakefs_s3, zitadel,
ray, mlflow. Dev auth is header-based (`auth.dev_headers` in `local.yaml`/`compose.yaml`):

```bash
curl -s 127.0.0.1:8090/v1/projects -H 'X-Tenant-Id: 11111111-1111-1111-1111-111111111111' -H 'X-User-Id: dev-user'
```

## Tests, migrations, lint

| Goal | Command |
|---|---|
| Unit tests (no services, no network) | `cd core-api && .venv/bin/python -m pytest tests -q` |
| Whole journey on a running stack (image import, train, deploy, predict, delete; slow) | `./scripts/e2e-journey.sh` |
| Migration status / roll back the last one | `docker compose run --rm dbmate status` / `... rollback` |
| Migrations from an empty PG18 as `backend_admin`, with rollback and re-up (Docker only, own throwaway project) | `./scripts/migrations-check.sh` |
| Lint migrations for destructive SQL | `python3 scripts/lint-migrations.py core-api/migrations/*.sql` |
| Logs of a service | `docker compose logs <service> --tail 50` |

## When something is broken

| Symptom | Cause and fix |
|---|---|
| `/health/deps` `secrets: error` naming a file | That file is missing: run `./scripts/bootstrap.sh` (native mode reads `.local/secrets-host/`, which only bootstrap writes) |
| `postgres: error` with an auth failure | `db.json` missing or its Vault lease gone (Vault restarted): `./scripts/bootstrap.sh` |
| `dbmate` exits non-zero, `core-api` never starts | `docker compose logs dbmate`. On a `pgdata` volume from before dbmate (no `backend_admin`, tables owned by `postgres`) the fix recreates only the `core` database (README "Migrations"). **Ask before dropping it.** |
| `ray`/`mlflow: error` natively, or `curl localhost:...` returns nothing | `localhost` tries `::1` first and OrbStack resets IPv6 for containers listening on IPv4 only (Ray, MLflow). Use `127.0.0.1`; `local.yaml` already does |
| `zitadel: error` | `zitadel.monorepo.orb.local` (Zitadel's external domain, kept from the old checkout name) comes from the `dev.orbstack.domains` label in `docker-compose.yml`: OrbStack must be running, and the container recreated after a label change |
| `uv run uvicorn` → "Failed to spawn", or `bad interpreter` naming another directory | The venv was created at an old checkout path: `cd core-api && uv sync --group dev --reinstall` |
| `uv run pytest` not resolving | rtk shim; use `.venv/bin/python -m pytest` |
| `Address already in use` on 8090 | The compose `core-api` container or an earlier uvicorn: `docker compose stop core-api` / `pkill -f "uvicorn app.main:app"` |
| Warning about orphan `core-backend-frontend-1` | Leftover from an old compose file: `docker compose up -d --remove-orphans` |

- `APP_ENV` is required: `local` (host process), `compose` (set by `docker-compose.yml`), `test` (set by `tests/conftest.py`).
- Do not run `docker compose down -v` without asking: Postgres (core, Zitadel, lakeFS, MLflow) and the Zitadel PAT live in those volumes.

## Ports

core-api 8090, Postgres 5432, MinIO 9000/9001, lakeFS 8000, Zitadel 8080, MLflow 5001, Ray 8265, Grafana 3000,
OTLP 4317, Vault 8200.

## Where things live

`scripts/` is what you run (`steps/`: the pieces `bootstrap.sh` runs, each runnable alone; `lib/secrets.sh`:
sourced). `compose/<service>/` holds files `docker-compose.yml` mounts into or builds for containers. `.local/`
(gitignored) holds `secrets/` and `secrets-host/`.
