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
.venv/bin/python -m pytest tests -q --cov=app --cov-report=term-missing:skip-covered  # line + branch coverage; CI fails under 90%
core-api/.venv/bin/ruff check .      # from the repo root (ruff is a core-api dev dep; config in ruff.toml, lint only)
./scripts/db-tests.sh                # from the repo root: tenant isolation as core_app on a real Postgres (Docker only)
./scripts/install-hooks.sh           # once per clone: pre-commit runs ruff on staged .py files
APP_ENV=local uv run uvicorn app.main:app --reload --port 8090   # services in compose, see below
```

Services: `docker compose up -d` with every service but `core-api` (or all of them to run it in compose), then
`./scripts/bootstrap.sh` (idempotent, run after every `up`). Native mode needs OrbStack. Skill: `run-local`.

CI (`.github/workflows/tests.yml`) runs the tests, ruff, the ray-jobs, ray-training and ray-serving tests and `db-tests.sh` on every pull request,
and `docker-publish.yml` builds no image until they pass. A new tenant table needs a seed line in
`core-api/tests/db/schema.py`, or `db-tests.sh` fails.

## Codebase map

Top level: `core-api/` (the FastAPI app, migrations, config, tests), three Ray-side packages that ship as zips
(`ray-jobs/`, `ray-training/`, `ray-serving/`), `task-bundles/tasks/<slug>/` (per-task trainers), `contracts/`
(`job-spec.v1.json`), `scripts/`, `compose/`, `test-console/` (a dev UI), `docs/`. The product console is the
separate `customer-frontend` repo. Start at `core-api/app/main.py` (router list, the one place that knows every
module) and `core-api/app/background.py` (every timer loop).

**`core-api/app/core/`** (shared kernel, imports nothing above it)
- `settings.py` + `secret_files.py`: config schema and secret JSON files. `db.py` (pool, follows rotated Vault creds), `tenant_conn.py` (`TenantConn`: a connection with `SET LOCAL app.tenant_id`, so RLS applies), `tenancy.py` + `auth.py` (token or dev-header -> tenant and actor), `authz/` (`policy.py` pure role -> allowed actions, `deps.py` `authorize`, `keys.py` project API keys).
- `problem.py` (RFC 9457 errors, every failure goes through it), `enums.py` (the StrEnums mirrored by DB CHECKs), `pagination.py` (keyset cursors), `idempotency.py` (Idempotency-Key replay and the capped create: tenant lock, plan-limit count, insert-once), `rate_limit.py` (Postgres-backed), `audit.py` / `admin_audit.py` (audit rows), `metrics/` and `tracing.py` (OTel).
- The training catalog lives here too: `task_types.py`, `task_catalog.py` (task enums, augmentations), `recipe.py` (the training recipe fields and validators, declared once; `presets.py` and `bundle_manifest.py` build on it), `requirements.py`, `job_packages.py` (registry of the Ray zips: candidate -> smoke -> stable, every move audited).

**`core-api/app/integrations/`** (one client per outside system)
- `compute.py`: submit (a `Submission`)/status/stop/logs of a Ray job, backend `ray_dashboard` or `rayjob` (config). `job_spec.py`: the pydantic job document (`kind` = train | ingest | predict_batch); its JSON Schema is `contracts/job-spec.v1.json`, regenerate with `scripts/gen-job-spec-schema.py`.
- `job_lifecycle.py`: the shared engine for long-running rows (`JobTable`: claim, compare-and-set `advance`, outbox `emit`/`emit_event`, sweep). Training runs, dataset imports and deployments each declare one (`training/persist.py`, `datasets/job_table.py`, `deployments/status.py`).
- `data_plane/`: the only code that touches lakeFS, the staging bucket and MLflow (`access.py` tenant checks, `layout.py` staging layout, `job_package.py` / `task_bundle.py` register and promote the Ray packages, `package_zip.py` / `package_store.py` build, validate and store their zips, `job_submit.py`, `train.py`, `ingest.py`).
- `serve_client.py` + `serve_config.py`: the two shared Ray Serve apps (`predictor-cpu`, `predictor-gpu`) and their declared config. Also `hub.py` (Hugging Face), `zitadel.py`, `mailer.py`.

**`core-api/app/modules/{user,admin}/<context>/`**: `user/` is the customer API (projects, datasets, training, model_registry, deployments incl. `/v1/predict`, api_keys, teams, tenants, tenant_members, webhooks, notifications, usage, ...), `admin/` the operator API (tenants, presets, task_types, task_bundles, job_packages, model_catalog, plan_limits, activity, audit_log, models). Beyond the `routes/service/schemas/constants` split (admin contexts too: a `service.py` takes a connection, writes and audits in one transaction), big contexts add `readers.py` / `writers.py` (SQL), a worker or reconciler (`reconcile.py`, `prediction_worker.py`, `promotion_worker.py`) and `lifecycle.py` / `status.py` state machines.

**Ray side** (own `pyproject.toml`, tests and zip; they cannot import each other, so a few constants and helpers are mirrored by hand and guarded by contract tests under `core-api/tests`, which puts all three on its `pythonpath`):
- `ray-jobs/ray_jobs`: dataset ingest (`entrypoints/{upload_images,upload_csv,huggingface}`), always writes `report.json` to a staging prefix.
- `ray-training/ray_core`: the training contract (Ray Train loop, recipe, store, `entrypoints/train.py`); staged under the package name `ray_training`. The `ray_training/` directory is an empty leftover. `task-bundles/tasks/<slug>/` hold one trainer per task, built by `scripts/build-bundle.sh`.
- `ray-serving/ray_serving`: the shared Serve apps and the `predict_batch` runner.
- core-api zips each package by content hash into the staging bucket (`jobs/<name>-<sha256>.zip`) and hands Ray a presigned URL; CI publishes new zips (`job-packages.yml`, `task-bundles.yml`) and an admin or auto-promote makes one stable.

**Flows.** Request: route -> `authorize` (policy + `TenantConn`) -> service -> `readers`/`writers` SQL under RLS. Long jobs: route writes the row and a job spec, `compute.submit` starts the Ray job, a poller in `background.py` (leader-locked per sweep by a Postgres advisory lock, also runnable alone as `python -m app.worker`) reconciles it through `job_lifecycle`, then audit, usage metering and events (outbox -> webhooks, `job_events`). Tests: `core-api/tests/` mirrors `app/` (`modules/`, `core/`, `integrations/`), plus `architecture/` (import rules, admin audit and shape checks, the feature-coverage manifest), `contract/` (OpenAPI snapshot, every route named by a test), `ingest_job/` (spec and report shapes shared with the Ray packages), `db/` (real Postgres, Docker only) and `support/` (`FakeConn`, `make_client`, `patch_tenant_conn`).
A new or changed route fails `contract/` until you regenerate the snapshot (`cd core-api && .venv/bin/python -m tests.contract.test_openapi_snapshot --write`) and name the route in a test (or add it to `UNTESTED` with a reason).

**Docs.** `README.md` (running the stack, migrations), `docs/runtime-contract.md` (the contract with the k8s repo), `docs/job-spec.md`, `docs/predict-api.md` (public predict API), `docs/datasets-plan.md` / `datasets-architecture.md`, `docs/trainer-plan.md`, `docs/ray-parallelism.md`, `docs/feature-coverage.md` (generated from `feature-coverage.yaml`, never hand-edit). Skills in `.claude/skills/` cover run-local, config-and-secrets, auth-layer, k8s-contract. Change behaviour that a doc describes in the same commit.

## Layout and rules

- `app/core` (settings, secrets, db, auth, tenancy) -> `app/integrations` (clients) -> `app/modules/{user,admin}/<context>` (routes). Imports go one way; `tests/architecture/test_architecture.py` enforces it. A module reaches another only through its package `__init__`.
- Repo tooling: `scripts/` holds what you run (`steps/` are the pieces `bootstrap.sh` runs, `lib/` is sourced);
  `compose/<service>/` holds files `docker-compose.yml` mounts into containers, never run on the host.
- **Config**: `core-api/config/*.yaml`, loaded by `app/core/settings.py`. Add a knob there plus `base.yaml`, never `os.environ`. Skill: `config-and-secrets`.
- **Secrets**: never in YAML, env or git. `config` holds the path of a JSON file; read it with `secret_files.get(name)`. Do not print secret values; inspect key names only.
- `APP_ENV` is required by the code (`local | compose | test | prod`); the Dockerfile sets `prod`. `auth.dev_headers` is refused in `prod`.
- **No magic strings or numbers.** A closed set of values (roles, actions, statuses, visibility, task types) is an `enum.StrEnum`/`IntEnum` (or a `TypedDict`/`Literal` for shapes) defined once in code and imported everywhere; never repeat a bare literal like `"owner"` or `3` in handlers or SQL parameters. A non-obvious number (limit, TTL) is a named constant or a config knob. Mirror each enum in the DB with a `CHECK` and keep a test that fails if the two drift.
- Every route needs tenant scoping through `TenantConn` (Postgres RLS). New tables get RLS in their migration.
- A new route also needs: an entry in `docs/feature-coverage.yaml` for the training, registry, deployment, serving, predict, jobs and datasets routes (`tests/architecture/test_feature_coverage.py`, then regenerate the `.md`); an `admin_audit` call for every mutating `/v1/admin` route (`test_admin_audit_coverage.py`); a refreshed OpenAPI snapshot.
- Migrations are dbmate files in `core-api/migrations/` named `<UTC YYYYMMDDHHMMSS>_<name>.sql`
  (`-- migrate:up` / `-- migrate:down`). Take the timestamp from `date -u +%Y%m%d%H%M%S` when you create the
  file, never a round number; it must sort after the newest existing version (if the clock is behind it, use
  newest + a few seconds). Never type a version by hand or reuse one you saw; run `date -u` at the moment you
  create the file, in every branch and every parallel agent. After merging branches, check for duplicates with
  `python3 scripts/lint-migrations.py core-api/migrations/*.sql` (it needs the file paths; with none it checks
  nothing) and rename the unpushed one. A migration that is pushed is frozen: never edit, rename or delete it, add a new one.
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

## Docs-vs-code audit

`../audit-report-coverage-2026-11-10.html` is generated from `../audit-refresh/` (verdicts + `apply.py`), never hand-edited.
Change a verdict in `verdicts/*.jsonl`, run `check_verdicts.py` then `apply.py` (`--selftest` first, expects `ok 47 441 9`);
wording lives in `prose.py`. Steps: `../audit-refresh/handoff-audit-report-refresh.md`. Subagents run on Opus or Sonnet only,
at most 3 at once. When code and planning docs diverge, judge which is the better long-term design: if the code is, amend
`../planning/` rather than the code.

## Commits

Conventional style (`feat(scope):`, `fix(scope):`, `chore(scope):`), imperative, explaining the why. No
Claude co-author trailer. Stage files by name.
