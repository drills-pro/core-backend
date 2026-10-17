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
| `prod.yaml` | `APP_ENV=prod` (the cluster): identity facts such as the Zitadel issuer and project id |
| `/etc/core-api/deploy.yaml` | prod only, **required**: the k8s repo's ConfigMap `backend-config` with the cluster's service hostnames (Postgres, Vault, Ray, MLflow, OTel) |
| `compose.yaml` | `APP_ENV=compose` (core-api inside docker compose) |
| `local.yaml` | `APP_ENV=local` (`uv run uvicorn` on the host against compose services) |
| `test.yaml` | `APP_ENV=test` (pytest sets it in `tests/conftest.py`) |

Precedence, lowest to highest: `base.yaml`, `<APP_ENV>.yaml`, the deploy file, then `CORE_<SECTION>__<KEY>` environment
variables (for example `CORE_PG__HOST`). The schema is `app/core/settings.py`; unknown keys are an error.

`APP_ENV` is required by the code and set to `prod` by the Dockerfile, so the cluster needs no
setting for it; compose sets `compose` and host runs set `local`. In `prod`, `auth.dev_headers` and
relative secret paths are refused at startup.

**k8s side:** the deployment mounts ConfigMap `backend-config` at `/etc/core-api/` (hostnames live there, next to the manifests that define them). The deployment's old env vars (`PG_HOST`, `RAY_DASHBOARD`,
`VAULT_ADDR`, ...) are no longer read and can be removed; their values now live in `prod.yaml`.
`OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_METRICS_EXPORTER` are still read by the OpenTelemetry SDK itself.

## Secrets: JSON files, referenced by path

Config never holds a secret value. `secrets:` in `base.yaml` names a file per secret and
`app/core/secret_files.py` reads it, again whenever its mtime changes, so rotation needs no restart.

| Secret | Cluster path | Keys |
|---|---|---|
| `lakefs` | `/var/app-secret/app-backend/lakefs` | `endpoint`, `access_key`, `secret_key` |
| `labelstudio` | `/var/app-secret/app-backend/labelstudio` | `url`, `refresh_token` |
| `zitadel` | `/var/app-secret/app-backend/zitadel` | `USER_ID`, `KEY_ID`, `PRIVATE_KEY` |
| `zitadel_oidc` | `/var/app-secret/app-backend/zitadel-oidc` | optional in prod (project id is in `prod.yaml`); local dev fallback |

In the cluster the Vault Agent Injector renders one file per key under `app-secret/app-backend/*`
(`deployment.yaml` template). Locally `scripts/secrets-init.sh` and the bootstrap scripts write the same
shapes to `.local/secrets/` (gitignored); compose mounts that directory at the cluster path.

The one thing still read from Vault's API is the **dynamic Postgres credential**
(`database/creds/core-app`), using the token file at `vault.token_file` (`/vault/secrets/token`).

## Other expectations of the cluster

- Probes are `tcpSocket` today. `/health` (liveness) and `/health/deps` exist; the auth plan adds `/ready`.
- Postgres is the per-app CNPG cluster `pg-backend` (host in `prod.yaml`); Vault's `core-app` role must point at the same cluster or logins fail with "password authentication failed".
- Egress used: Postgres, Vault, OTel gateway, Ray dashboard, lakeFS, MLflow, Label Studio, Zitadel.
- Migrations: `core-api/migrations/*.sql`, applied by hand as the `postgres` superuser (README), not by the app or an init container.

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
