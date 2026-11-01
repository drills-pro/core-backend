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
ruff check .                         # from the repo root (mise installs ruff; config in ruff.toml, lint only)
./scripts/install-hooks.sh           # once per clone: pre-commit runs ruff on staged .py files
APP_ENV=local uv run uvicorn app.main:app --reload --port 8090   # services in compose, see below
```

Services: `docker compose up -d` with every service but `core-api` (or all of them to run it in compose), then
`./scripts/bootstrap.sh` (idempotent, run after every `up`). Native mode needs OrbStack. Skill: `run-local`.

## Layout and rules

- `app/core` (settings, secrets, db, auth, tenancy) -> `app/integrations` (clients) -> `app/modules/{user,admin}/<context>` (routes). Imports go one way; `tests/architecture/test_architecture.py` enforces it.
- Repo tooling: `scripts/` holds what you run (`steps/` are the pieces `bootstrap.sh` runs, `lib/` is sourced);
  `compose/<service>/` holds files `docker-compose.yml` mounts into containers, never run on the host.
- **Config**: `core-api/config/*.yaml`, loaded by `app/core/settings.py`. Add a knob there plus `base.yaml`, never `os.environ`. Skill: `config-and-secrets`.
- **Secrets**: never in YAML, env or git. `config` holds the path of a JSON file; read it with `secret_files.get(name)`. Do not print secret values; inspect key names only.
- `APP_ENV` is required by the code (`local | compose | test | prod`); the Dockerfile sets `prod`. `auth.dev_headers` is refused in `prod`.
- **No magic strings or numbers.** A closed set of values (roles, actions, statuses, visibility, task types) is an `enum.StrEnum`/`IntEnum` (or a `TypedDict`/`Literal` for shapes) defined once in code and imported everywhere; never repeat a bare literal like `"owner"` or `3` in handlers or SQL parameters. A non-obvious number (limit, TTL) is a named constant or a config knob. Mirror each enum in the DB with a `CHECK` and keep a test that fails if the two drift.
- Every route needs tenant scoping through `TenantConn` (Postgres RLS). New tables get RLS in their migration.
- Migrations are dbmate files in `core-api/migrations/` named `<UTC YYYYMMDDHHMMSS>_<name>.sql`
  (`-- migrate:up` / `-- migrate:down`). Take the timestamp from `date -u +%Y%m%d%H%M%S` when you create the
  file, never a round number; it must sort after the newest existing version (if the clock is behind it, use
  newest + a few seconds). A migration that is pushed is frozen: never edit, rename or delete it, add a new one.
  Only an unpushed migration may be amended or renamed. They run as `backend_admin` (database owner, no superuser, no
  BYPASSRLS) with no default privileges: grant `core_app` every new table, sequence and function, ENABLE
  (not FORCE) RLS, pin `search_path` on SECURITY DEFINER, never create roles. `docker compose up` applies
  them; check with `./scripts/migrations-check.sh` (README "Migrations").

## Code style (applies to every session and coding agent)

1. **No magic strings or numbers.** Audit actions, entity types, problem codes, header names, limits, TTLs and
   default names are named: an `enum.StrEnum`/`IntEnum` for a closed set, a module constant (or a config knob)
   for a single value. Defined once, imported everywhere, never repeated as a literal in a handler, SQL
   parameter or test. (Detail and DB mirroring: the "No magic strings" rule above.)
2. **Modular code.** A package under `app/modules/**` splits by job: `routes.py` is HTTP only (parse, authorize,
   call, shape the response), `service.py` holds the business rules and queries, `schemas.py` the pydantic
   models, `constants.py` the enums and named values. A function does one thing; a handler does not grow past
   a screen. Do not put logic in `routes.py` that a second caller would need.
3. **Typed structures are pydantic.** Request bodies, response shapes (`response_model=`), service inputs and
   outputs that cross a function boundary are `pydantic.BaseModel`s, not loose `dict`s. Reuse and extend the
   existing models before adding a new one; use `model_validate` on DB rows. Settings stay in `settings.py`.
   `TypedDict`/`Literal` only where a model would be pure overhead (e.g. a single fixed-shape enum value).

Apply these to code you add or touch; do not rewrite unrelated code to match (surgical changes).

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
