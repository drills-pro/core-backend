# core-backend (core-api)

FastAPI modular monolith for the drills.pro no-code ML platform. Production is the `k8s` repo (sibling
directory, Flux + Kustomize on a 2-node k3s). Design docs are in `../planning/` (auth and tenancy:
`04-identity-tenancy-authz.md`, sprint specs `21` and `23`). This repo owns code, image, migrations and
config; the `k8s` repo owns manifests, Vault policies and versions.

## Commands

Run from `core-api/` (tools via `mise`; ask before installing anything):

```bash
uv sync --group dev                  # first time
.venv/bin/python -m pytest tests -q  # tests need no services and no network
APP_ENV=local uv run uvicorn app.main:app --reload --port 8090   # services in compose, see below
```

Services: `docker compose up -d` with every service but `core-api` (or all of them to run it in compose), then
`./scripts/bootstrap.sh` (idempotent, run after every `up`). Native mode needs OrbStack. Skill: `run-local`.

## Layout and rules

- `app/core` (settings, secrets, db, auth, tenancy) -> `app/integrations` (clients) -> `app/modules/{user,admin}/<context>` (routes). Imports go one way; `tests/test_architecture.py` enforces it.
- Repo tooling: `scripts/` holds what you run (`steps/` are the pieces `bootstrap.sh` runs, `lib/` is sourced);
  `compose/<service>/` holds files `docker-compose.yml` mounts into containers, never run on the host.
- **Config**: `core-api/config/*.yaml`, loaded by `app/core/settings.py`. Add a knob there plus `base.yaml`, never `os.environ`. Skill: `config-and-secrets`.
- **Secrets**: never in YAML, env or git. `config` holds the path of a JSON file; read it with `secret_files.get(name)`. Do not print secret values; inspect key names only.
- `APP_ENV` is required by the code (`local | compose | test | prod`); the Dockerfile sets `prod`. `auth.dev_headers` is refused in `prod`.
- Every route needs tenant scoping through `TenantConn` (Postgres RLS). New tables get RLS in their migration.
- Migrations are dbmate files in `core-api/migrations/` named `<UTC YYYYMMDDHHMMSS>_<name>.sql`
  (`-- migrate:up` / `-- migrate:down`). They run as `backend_admin` (database owner, no superuser, no
  BYPASSRLS) with no default privileges: grant `core_app` every new table, sequence and function, ENABLE
  (not FORCE) RLS, pin `search_path` on SECURITY DEFINER, never create roles. `docker compose up` applies
  them; check with `./scripts/migrations-check.sh` (README "Migrations").

## Cross-repo

A change to `config/prod.yaml`, secret file names or keys, the image's port or probes needs a matching
`k8s` change (skill `k8s-contract`). Do not edit or push the `k8s` repo from here without being asked: a
push there deploys to production.

## Auth rework

Shared pool is in (ADR-021): the backend verifies the Zitadel token and reads tenant membership from
Postgres; the BFF cookie session (ADR-024) and `platform_admins` are not. Skill: `auth-layer`.

## Commits

Conventional style (`feat(scope):`, `fix(scope):`, `chore(scope):`), imperative, explaining the why. No
Claude co-author trailer. Stage files by name.
