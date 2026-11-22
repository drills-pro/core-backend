# core-backend

Backend of the drills.pro no-code ML platform: `core-api` (FastAPI, Postgres with row-level security), the Ray-side
packages it ships to the cluster (`ray-jobs`, `ray-training`, `ray-serving`), the per-task trainers in `task-bundles/`,
and a local stack in docker compose. Design docs live in the sibling `planning/` repo; production manifests in the
sibling `k8s` repo. Working notes for agents and a map of the code: [CLAUDE.md](CLAUDE.md).

## Run

```
mise install
./scripts/secrets-init.sh   # local secret files, once
docker compose up --build
./scripts/bootstrap.sh
```

**Run `./scripts/bootstrap.sh` after every `docker compose up`, not just the first time.** Vault runs in dev mode,
which keeps everything in memory: restarting that one container empties it, and `bootstrap.sh` writes a fresh Postgres
credential (`.local/secrets/db.json`) from it. Every step is idempotent. It finishes by printing `/health/deps`.

On a brand-new Zitadel volume run `./scripts/zitadel-bootstrap.sh` once before `bootstrap.sh`: it creates the Zitadel
project, OIDC apps and roles through the Management API and would create duplicates if re-run.

### core-api natively on the Mac, everything else in Docker

Needs OrbStack (the host copies of the secret files use its `<service>.<project>.orb.local` names).

```bash
mise install
(cd core-api && uv sync --group dev)
./scripts/secrets-init.sh
docker compose up -d postgres minio lakefs zitadel ray-head lgtm mlflow vault   # all but core-api; dbmate migrates
./scripts/bootstrap.sh          # also writes .local/secrets-host/ for config/local.yaml
cd core-api && APP_ENV=local uv run uvicorn app.main:app --reload --port 8090
```

The `run-local` skill (`.claude/skills/run-local/SKILL.md`) has the full routine and a troubleshooting table.

| Service | URL |
|---|---|
| core-api | http://localhost:8090/health/deps |
| core-api docs (OpenAPI) | http://localhost:8090/docs |
| Ray dashboard | http://localhost:8265 |
| Ray Serve proxy (host port) | http://localhost:8100 |
| MinIO console | http://localhost:9001 |
| LakeFS | http://localhost:8000 |
| Zitadel | http://zitadel.monorepo.orb.local:8080 (OrbStack domain) |
| MLflow | http://localhost:5001 |
| Grafana (LGTM) | http://localhost:3000 |
| Vault | http://localhost:8200 |

`GET /health` is the liveness signal. `GET /health/deps` reports `postgres`, `secrets`, `lakefs_s3`, `zitadel`, `ray`
and `mlflow`, each `ok` or `error: ...`.

`./scripts/e2e-journey.sh` drives the whole path on a running local stack with the dev headers: a tiny
image-classification project, zip upload, ingest, commit, a real training run, deploy, a predict call, then delete.
It needs Ray able to train and serve, so a run takes minutes. `API`, `TENANT`, `POLLS` and `POLL_SECONDS` override the
defaults. `scripts/e2e-import.sh` covers import only and `scripts/e2e-train-deploy.sh` is the fast variant that seeds
its training runs in Postgres.

## Development

```
cd core-api
uv sync --group dev
.venv/bin/python -m pytest tests -q       # no services, no network; CI fails under 90% coverage
core-api/.venv/bin/ruff check .           # from the repo root
./scripts/db-tests.sh                     # from the repo root: tenant isolation as core_app on a real Postgres (Docker)
```

CI is `.github/workflows/tests.yml` (core-api tests and ruff, the ray-jobs, ray-training and ray-serving suites, and
`db-tests.sh`) on every pull request; `docker-publish.yml` builds the image only after it passes. The other workflows
publish the Ray images (`ray-image.yml`, `ray-jobs-image.yml`, `ray-training-image.yml`), the Ray code zips
(`job-packages.yml`), the task bundles (`task-bundles.yml`) and the test console (`test-console-image.yml`).

### Layout (modular monolith)

One deployable, split into bounded contexts with a one-directional dependency graph:

```
core-api/app/
  main.py          composition root: mounts every module's router, nothing else
  background.py    the pollers: each module owns the reconcile, this owns the timer
  worker.py        the same pollers as a process of their own (python -m app.worker)
  core/            shared kernel: settings, secrets, db, tenancy/RLS, auth, authz, audit, problems, metrics,
                   and the training catalog (task types, presets, job package registry)
  integrations/    one client per outside system: Ray (compute), job specs, Ray Serve, lakeFS/S3/MLflow
                   (data_plane/), Hugging Face, Zitadel, mail
  modules/
    user/<context>/    the customer API: projects, datasets, training, model_registry, deployments, api_keys,
                       teams, tenants, tenant_members, webhooks, notifications, usage, ...
    admin/<context>/   the operator API (/v1/admin/...): tenants, presets, task types and bundles, job packages,
                       model catalog, plan limits, activity, audit log, models
      routes.py  schemas.py  constants.py   HTTP surface, pydantic models, enums
      service.py                            business rules shared by HTTP and background callers
      __init__.py                           the public API: what other modules may import
```

The rules: `core/` depends on nothing above it, `integrations/` may use `core/`, and a module reaches another module
only through its package root. `tests/architecture/test_architecture.py` fails the build on any violation. Every
setting is declared in `app/core/settings.py` and set in `core-api/config/*.yaml` (see
[docs/runtime-contract.md](docs/runtime-contract.md)); secrets are JSON files referenced by path, never values.

## Multi-tenancy

Postgres row-level security, not just an app-layer `WHERE tenant_id = ...`. `core_app` is a `NOLOGIN` group role that
holds the grants; nothing connects as it directly (the pool logs in as a Vault-issued user that is a member). Every
tenant-scoped table has an RLS policy reading `app.tenant_id`, set per request with `SET LOCAL` by
`app/core/tenant_conn.py` (`TenantConn`). Cross-tenant reads (existence checks, the admin API) go through
`SECURITY DEFINER` functions that return only what they must. `./scripts/db-tests.sh` proves the isolation as
`core_app` on a real Postgres. The init migration seeds two demo tenants (`11111111-...`, `22222222-...`).

## Auth

`app/core/auth.py` verifies Zitadel-issued JWTs (JWKS fetched and cached; RS256 signature, issuer, and the project id
as audience). Tenant membership and roles come from Postgres, not from token claims (`app/core/tenancy.py`,
`app/core/authz/`). Project API keys (`drl_...`) authenticate the predict API. In local and compose runs
`auth.dev_headers: true` accepts `X-Tenant-Id` / `X-User-Id` (and `X-Admin-User` for `/v1/admin`) instead of a token;
the code refuses that setting when `APP_ENV=prod`.

```
curl -s 127.0.0.1:8090/v1/projects -H 'X-Tenant-Id: 11111111-1111-1111-1111-111111111111' -H 'X-User-Id: dev-user'
```

For a real token (device flow) use `scripts/dev-token.sh`, see its header for the one-time Zitadel app. Design:
`../planning/04-identity-tenancy-authz.md`; skill `auth-layer`.

## Datasets

Data enters through imports and every successful import is one immutable version. Decisions and deferred work:
[docs/datasets-plan.md](docs/datasets-plan.md); diagrams and lifecycle: [docs/datasets-architecture.md](docs/datasets-architecture.md).
All lakeFS, staging-bucket and MLflow access goes through `app/integrations/data_plane/`. A dataset takes its
project's HF task slug; the supported ones are the enabled rows of `task_types` (managed through
`/v1/admin/task-types`, bundles in `task-bundles/tasks/`), anything else answers 422 "not supported yet".

Flow, all under `/v1/projects/{project_id}/datasets`:

```
POST   /                                            create a dataset
POST   /{id}/imports                                create an import, returns a presigned PUT to staging
PUT    <presigned url> --data-binary @data.zip      the client uploads straight to staging
POST   /{id}/imports/{imp}/start                    queue the Ray ingest job (no bytes through the API)
GET    /{id}/imports/{imp}                          poll: running -> awaiting_review (report + sample)
GET    /{id}/imports/{imp}/preview                  sample rows and presigned URLs from the staged index
POST   /{id}/imports/{imp}/commit                   lakeFS commit -> new version (or .../discard, .../cancel)
GET    /{id}/versions                               list versions (cursor pagination)
GET    /{id}/versions/{v}/preview | /files/{path}   rows and files of a committed version
PUT    /{id}/published                              publish a version; DELETE unpublishes
DELETE /{id}                                        soft delete, 7-day retention; POST /{id}:restore
```

An import may carry `base_version_id` to merge on top of an earlier version (strict: exact duplicates are skipped, any
other conflict fails the import with a report; `409 stale_base` if the base is no longer the latest). Archive rules and
size limits are the `dataset_limits:` knobs in `config/base.yaml`. The ingest job is the `ray_jobs` package in
`ray-jobs/` (one entrypoint per source) and writes only to a staging prefix.

## Training, models and serving

- **Training.** `POST /v1/projects/{id}/training-runs` takes a dataset version and a preset, writes a `train` job spec
  and submits it through `app/integrations/compute.py` (Ray job API or a KubeRay `RayJob`). The driver is `ray_core`
  (`ray-training/`), running the task's bundle from `task-bundles/tasks/<slug>/`. A poller reconciles the run, reads
  `report.json`, logs to MLflow and meters usage. Presets and task types are admin-managed rows
  (`/v1/admin/training-presets`, `/v1/admin/task-types`). Design: `docs/trainer-plan.md`, `docs/job-spec.md`.
- **Model registry.** `model_registry` registers a finished run as a model version in MLflow (core-api is the only
  door customers have to it) and promotes it; a promotion to Production is deployed by a background worker.
- **Deployments and predict.** Two long-lived Ray Serve apps (`predictor-cpu`, `predictor-gpu`, package
  `ray-serving/`) multiplex every deployed model. Customers call `POST /v1/predict` with a project API key (sync,
  or `Prefer: respond-async` for background predictions): [docs/predict-api.md](docs/predict-api.md). The older
  `POST /v1/predict/{deployment_id}` with `X-Predict-Key` still works and is deprecated. Rate limits are per key, per
  plan and per deployment (`app/core/rate_limit.py`, counted in Postgres).
- **Ray code ships as zips.** core-api (and CI) stage `ray_jobs`, `ray_core` and `ray_serving` in the staging bucket by
  content hash; a package is a candidate until its smoke job passes and it is made stable
  (`/v1/admin/job-packages`, `/v1/admin/task-bundles`). Details: `docs/runtime-contract.md`.
- Where Ray parallelism is used: [docs/ray-parallelism.md](docs/ray-parallelism.md). What is built and tested:
  [docs/feature-coverage.md](docs/feature-coverage.md).

## Observability

`app/core/tracing.py` instruments core-api (FastAPI, httpx, asyncpg) with OpenTelemetry and exports to the LGTM
stack's OTLP collector (`grafana/otel-lgtm`: Tempo, Loki, Prometheus, Grafana). Spans carry `tenant_id` once a request
resolves its tenant. Metrics (`app/core/metrics/`) are named `drills.core_backend.<namespace>.<name>`, for example
`drills.core_backend.training.runs{status}` and `drills.core_backend.inference.latency`. Database size comes from the
Postgres exporter, not core-api. Alerts and dashboards live in the `k8s` repo.

## LakeFS one-time setup

On first boot LakeFS has no admin user and there are no buckets. `scripts/steps/lakefs.sh` creates the admin user, the
blockstore bucket and the staging bucket (plus a bucket-scoped `ingest` MinIO user). `bootstrap.sh` runs it; it is
idempotent. core-api creates one lakeFS repository per dataset itself.

## Secrets and configuration

Non-secret settings are committed YAML (`core-api/config/base.yaml`, plus `local`, `compose`, `test`, `prod`);
`APP_ENV` picks the overlay and `CORE_<SECTION>__<KEY>` env vars override. Secrets are JSON files whose *paths* are in
the config (`secrets:`: `db`, `lakefs`, `staging`, `ingest`, `zitadel`, `zoho`, `webhooks`). `core-api` re-reads a file
when it changes.

| | Cluster | Local |
|---|---|---|
| Files | `/var/app-secret/app-backend/<name>`, rendered by the Vault Agent Injector | `.local/secrets/<name>.json` (gitignored), mounted at the same path in compose |
| Written by | `k8s/bootstrap/04-vault-seed-secrets.sh` | `scripts/secrets-init.sh` |

`core-api` does not talk to Vault. Its Postgres credential is the `db` secret file (`{"username", "password"}`): in the
cluster the Vault agent issues it from `database/creds/core-app` and rewrites the file when it needs a new user;
locally `./scripts/bootstrap.sh` reads one from the dev Vault (dev mode: in-memory, root token `root`, laptop-only).
`app/core/db.py` checks the file every 30s and, when it changed, opens a pool with the new user and closes the old one.

Everything local uses fixed, well-known dev values (MinIO's published example keys, a fixed Zitadel masterkey,
Vault's dev root token). **Not for anything but a laptop.** Skill: `config-and-secrets`.

## Migrations

`core-api/migrations/<UTC timestamp>_<name>.sql` in [dbmate](https://github.com/amacneil/dbmate) format
(`-- migrate:up` / `-- migrate:down`); the timestamp is the version. A new one is named
`$(date -u +%Y%m%d%H%M%S)_<name>.sql`; a pushed one is frozen. The image ships them at `/migrations`; in the cluster
the `backend-migrate` Job applies them before a new image rolls out (`docs/runtime-contract.md`).

They run as `backend_admin`, the role k8s already provisions for this app: owner of the database, not a
superuser, no `BYPASSRLS`, and no default privileges. So a migration that adds a table, sequence or
function also `GRANT`s what `core_app` needs; tables get `ENABLE ROW LEVEL SECURITY` (not `FORCE`: the
owner must skip the policies so `SECURITY DEFINER` functions can read across tenants; `core_app` owns
nothing, so the policies always apply to it); every `SECURITY DEFINER` function pins `SET search_path =
public, pg_temp` and filters rows itself; nothing creates roles or needs superuser rights. Never grant on
`schema_migrations`.

`docker compose up` does the local part: a fresh `pgdata` volume runs `compose/postgres/core-api.sql`
(`backend_admin`, `core_app`, the `core` database owned by `backend_admin`), then the `dbmate` service
applies pending migrations as `backend_admin`, and `core-api` starts only after it succeeds.

| Goal | Command |
|---|---|
| Status / roll back the last one | `docker compose run --rm dbmate status` / `... rollback` |
| Prove a change from an empty PG18 as `backend_admin`, including rollback and re-up | `./scripts/migrations-check.sh` (Docker only; uses the compose services in a throwaway project; CI runs it on every pull request and before building the image) |
| Prove tenant isolation: cross-tenant select, update and delete as `core_app` on every tenant table | `./scripts/db-tests.sh [pytest args]` (Docker only; a throwaway project like the check above; runs `core-api/tests/db`; CI runs it on every pull request) |
| Lint for destructive SQL (DROP TABLE/COLUMN, TRUNCATE, ALTER COLUMN TYPE without USING, DELETE without WHERE) | `python3 scripts/lint-migrations.py core-api/migrations/*.sql`; opt out per statement with `-- migration-lint: allow-destructive (why)` |

**Volumes from before dbmate.** Init scripts only run on an empty volume, so an existing `pgdata` has no
`backend_admin` and its `core` tables belong to `postgres`; the `dbmate` service fails and `core-api`
does not start. Recreate only `core` (Zitadel, lakeFS and MLflow keep their databases):

```bash
docker compose exec postgres psql -U postgres -c 'DROP DATABASE core WITH (FORCE)'
docker compose exec -T postgres psql -U postgres < compose/postgres/core-api.sql
docker compose up -d
```

Your Zitadel org's tenant and Default team rows lived in that database: re-run the two `INSERT`s under
"Seed the tenant row" in `scripts/zitadel-bootstrap.sh` (not the whole script, which would duplicate the
Zitadel apps).
