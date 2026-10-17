# Runtime contract: core-api and the cluster

The `k8s` repo deploys this image with Flux and Kustomize (`k8s/apps/backend/`). Nothing is templated
with Helm. This page is the whole interface between the two repos: change a row here and the other
repo needs a matching change.

## Image

`ghcr.io/drills-pro/core-backend/core-api`, port 8000, built from `core-api/Dockerfile` by
`.github/workflows/docker-publish.yml`, which calls `k8s/.github/workflows/bump-image.yml` with the
digest. The k8s repo pins by digest.

## Configuration: `core-api/config/*.yaml`

| File | Used when |
|---|---|
| `base.yaml` | always; defaults and the cluster's secret file paths |
| `prod.yaml` | `APP_ENV=prod` (the cluster): all of its hostnames, the Zitadel issuer and project id |
| `compose.yaml` | `APP_ENV=compose` (core-api inside docker compose) |
| `local.yaml` | `APP_ENV=local` (`uv run uvicorn` on the host against compose services) |
| `test.yaml` | `APP_ENV=test` (pytest sets it in `tests/conftest.py`) |

Precedence, lowest to highest: `base.yaml` (shared defaults), `<APP_ENV>.yaml`, then `CORE_<SECTION>__<KEY>` environment
variables (for example `CORE_PG__HOST`). The schema is `app/core/settings.py`; unknown keys are an error.

`APP_ENV` is required by the code and set to `prod` by the Dockerfile, so the cluster needs no
setting for it; compose sets `compose` and host runs set `local`. In `prod`, `auth.dev_headers` and
relative secret paths are refused at startup.

**k8s side:** nothing is needed. The deployment's old env vars (`PG_HOST`, `RAY_DASHBOARD`,
`VAULT_ADDR`, ...) are no longer read and can be removed; their values now live in `prod.yaml`.
`OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_METRICS_EXPORTER` are still read by the OpenTelemetry SDK itself.

## Secrets: JSON files, referenced by path

Config never holds a secret value. `secrets:` in `base.yaml` names a file per secret and
`app/core/secret_files.py` reads it, again whenever its mtime changes, so rotation needs no restart.

| Secret | Cluster path | Keys |
|---|---|---|
| `db` | `/vault/secrets/db` (k8s `apps/_components/vault-db-creds`, from `database/creds/core-app`) | `username`, `password`; the pool switches when the file changes |
| `lakefs` | `/var/app-secret/app-backend/lakefs` | `endpoint`, `access_key`, `secret_key` |
| `labelstudio` | `/var/app-secret/app-backend/labelstudio` | `url`, `refresh_token` |
| `zitadel` | `/var/app-secret/app-backend/zitadel` | `USER_ID`, `KEY_ID`, `PRIVATE_KEY` |
| `zitadel_oidc` | `/var/app-secret/app-backend/zitadel-oidc` | optional in prod (project id is in `prod.yaml`); local dev fallback |

In the cluster the Vault Agent Injector renders one file per key under `app-secret/app-backend/*`
(k8s `apps/_components/vault-agent`, derived from the ServiceAccount `app-backend`). Locally `scripts/secrets-init.sh` and the bootstrap scripts write the same
shapes to `.local/secrets/` (gitignored); compose mounts that directory at the cluster path.

core-api makes no Vault API calls: the Vault agent sidecar renews the Postgres lease and rewrites
`/vault/secrets/db` when it has to issue a new user. The k8s side keeps `agent-inject-token` only until
this image is deployed; it can be removed after.

## Other expectations of the cluster

- Probes are `tcpSocket` today. `/health` (liveness) and `/health/deps` exist; the auth plan adds `/ready`.
- Postgres is the per-app CNPG cluster `pg-backend` (host in `prod.yaml`); Vault's `core-app` role must point at the same cluster or logins fail with "password authentication failed".
- Egress used: Postgres, OTel gateway (the Vault agent sidecar, not core-api, talks to Vault), Ray dashboard, lakeFS, MLflow, Label Studio, Zitadel.
- Migrations: see the next section.

## Migrations: `/migrations` in the image

The image holds `/migrations/*.sql` (from `core-api/migrations/`) in dbmate format and a `cp` binary, and
no migration runner. On every new digest the k8s Job `platform/app-backend-migrate` (`k8s/apps/backend/migrate`,
gate in `k8s/apps/backend/flux.yaml`) runs before the Deployment rolls out:

1. an init container from this image: `cp -R /migrations/. /work/`;
2. `ghcr.io/amacneil/dbmate:2.36.0`: `dbmate --env-file /vault/secrets/dbmate.env --migrations-dir /migrations
   --migrations-table schema_migrations --wait --no-dump-schema up`. `backoffLimit: 0`, 600 s deadline; on
   failure the old pods keep serving.

dbmate wraps each file in a transaction; no file uses `transaction:false`.

**Roles.** The Job logs in to the `backend` database as a 1h Vault user (`database/creds/core-migrator`)
that switches at login to `backend_owner`: `NOLOGIN`, `CREATEROLE`, `BYPASSRLS`, not a superuser; a member
of `backend_admin` (the database owner) and ADMIN on `core_app`. Every object, and `schema_migrations`,
is owned by `backend_owner`. `core_app`, the runtime group role, owns nothing, so RLS applies to it.

**Rules for a migration.**
- No default privileges: each new table, sequence and function `GRANT`s what `core_app` needs in the same
  file. Never grant on `schema_migrations`. `core_app` has `USAGE` on `public`, not `CREATE`.
- `SECURITY DEFINER` functions run as `backend_owner`, so they bypass RLS: each one sets
  `search_path = public, pg_temp`, filters rows itself, and is `REVOKE`d from `PUBLIC`.
- Keep `FORCE ROW LEVEL SECURITY`. Data migrations over RLS tables work through `BYPASSRLS`.
- Nothing superuser-only (`CREATE EXTENSION`, ...), and no `RESET ROLE` (objects would belong to the
  short-lived user, and Vault's `DROP ROLE` would fail).
- `scripts/migrations-check.sh` proves all of this from an empty PG18 with these roles
  (`scripts/db-roles.sql`), and CI runs it before building the image.

**k8s side:** the one-time "Migrator role" steps in `k8s/docs/operations.md`, then the `backend-migrate`
Kustomization in `k8s/apps/backend/flux.yaml`. A new migration needs nothing in k8s.

## Models, experiments and MLflow

MLflow is internal (NetworkPolicy: `platform` namespace only, plus the `ray` namespace for artifact
transfer) and core-api is the only door customers have to it.

- **Isolation:** experiments are `tenants/<tenant>/projects/<project>`, registered models are
  `<tenant>.<project>.<name>`. Routes reach MLflow ids only through `training_runs` and
  `model_versions` rows (RLS plus the project filter), never from client input.
- **Run at submit:** `POST .../training-runs` creates the MLflow run, stores `mlflow_run_id`, and passes
  the run's artifact path to the job. core-api logs metrics and closes the run when the job ends.
- **Trainer upload:** the Ray job `PUT`s `model.onnx` and `model.json` to MLflow's artifact API
  (`/api/2.0/mlflow-artifacts/artifacts/<exp>/<run>/artifacts/model/...`). MLflow's `--serve-artifacts`
  stores them in the bucket, so core-api and the trainers hold no storage credential for models.
- **Serving:** the deployment script downloads the same artifact path from MLflow's artifact API.
- **k8s:** the `ray` namespace needs a NetworkPolicy allowing ingress to `app=mlflow` on port 5000.
