---
name: run-local
description: Run, test or debug core-api locally. Use when asked to start the backend, run tests, bring up the docker compose stack, or when a local dependency (Postgres, Vault, lakeFS, Zitadel, MinIO, Label Studio) looks broken.
---

# Run core-api locally

| Goal | Command (from `core-api/` unless noted) |
|---|---|
| Unit tests (no services) | `.venv/bin/python -m pytest tests -q` |
| Whole stack | `docker compose up --build` then `./scripts/bootstrap.sh` (repo root) |
| API on the host, services in compose | `./scripts/secrets-init.sh` once, then `APP_ENV=local uv run uvicorn app.main:app --reload` |
| What is wrong | `curl localhost:8090/health/deps` (compose) |

- `bootstrap.sh` is idempotent: run it after every `docker compose up`. Vault is in dev mode (in memory), so a restart loses the Postgres credential role until it runs.
- Secret files live in `.local/secrets/` (gitignored); compose mounts them where the cluster puts them. A `secrets` error in `/health/deps` names the missing file or key.
- First time only: `./scripts/zitadel-bootstrap.sh` (creates the Zitadel project and apps; re-running duplicates them).
- `uv run pytest` may not resolve under the rtk shim; use `.venv/bin/python -m pytest`.
- Do not tear down volumes (`docker compose down -v`) without asking: Postgres and Zitadel state live there.
