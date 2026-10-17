---
name: run-local
description: Set up, run, test or debug core-api locally. Use when asked to start the backend, set up a fresh checkout, run tests, bring up the docker compose stack, or when a local dependency (Postgres, Vault, lakeFS, Zitadel, MinIO, Label Studio) looks broken.
---

# Run core-api locally

Config and secrets model: skill `config-and-secrets`. This repo ships no UI; the console is the separate `customer-frontend` repo.

## Fresh checkout, full stack (in order)

```bash
mise install
./scripts/secrets-init.sh          # .local/secrets/lakefs.json + vault-token
docker compose up --build -d
./scripts/zitadel-bootstrap.sh     # first time only: creates the Zitadel project and apps (re-running duplicates them)
./scripts/bootstrap.sh             # vault DB engine, OIDC + Label Studio secrets, lakeFS repo; prints /health/deps
```

After that, every `docker compose up` is followed by `./scripts/bootstrap.sh` (idempotent). Vault is in dev mode (in memory): a restart loses the Postgres credential role until bootstrap runs. Static secret files live in `.local/secrets/` (gitignored) and survive.

## Everyday

| Goal | Command (from `core-api/`) |
|---|---|
| Unit tests (no services, no network) | `.venv/bin/python -m pytest tests -q` |
| API on the host, services in compose | `APP_ENV=local uv run uvicorn app.main:app --reload` |
| Which dependency is down | `curl localhost:8090/health/deps` (compose) |

- `APP_ENV` is required: `local` (host process), `compose` (set by docker-compose.yml), `test` (set by `tests/conftest.py`).
- The `secrets` entry in `/health/deps` names a missing file or key. `vault: error` means the dev Vault was restarted: run `./scripts/bootstrap.sh`.
- Host-run (`APP_ENV=local`) reaches lakeFS and MinIO through the hostnames inside the secret files, which only resolve on OrbStack; otherwise run core-api in compose.
- `uv run pytest` may not resolve under the rtk shim; use `.venv/bin/python -m pytest`.
- Do not run `docker compose down -v` without asking: Postgres and Zitadel state live in those volumes.

## Ports

core-api 8090, Postgres 5432, MinIO 9000/9001, lakeFS 8000, Zitadel 8080, MLflow 5001, Ray 8265, Grafana 3000, Vault 8200, Label Studio 8110.
