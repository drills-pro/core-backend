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
| `prod.yaml` | `APP_ENV=prod` (the cluster) |
| `compose.yaml` | `APP_ENV=compose` (core-api inside docker compose) |
| `local.yaml` | `APP_ENV=local` (`uv run uvicorn` on the host against compose services) |
| `test.yaml` | `APP_ENV=test` (pytest sets it in `tests/conftest.py`) |

Precedence, lowest to highest: `base.yaml`, `<APP_ENV>.yaml`, then `CORE_<SECTION>__<KEY>` environment
variables (for example `CORE_PG__HOST`). The schema is `app/core/settings.py`; unknown keys are an error.

`APP_ENV` is required by the code and set to `prod` by the Dockerfile, so the cluster needs no
setting for it; compose sets `compose` and host runs set `local`. In `prod`, `auth.dev_headers` and
relative secret paths are refused at startup.

**k8s side:** nothing is required for the config rework. The deployment's env vars (`PG_HOST`, `RAY_DASHBOARD`,
`VAULT_ADDR`, ...) are no longer read and can be removed; their values now live in `prod.yaml`.
`OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_METRICS_EXPORTER` are still read by the OpenTelemetry SDK itself.

## Secrets: JSON files, referenced by path

Config never holds a secret value. `secrets:` in `base.yaml` names a file per secret and
`app/core/secret_files.py` reads it, again whenever its mtime changes, so rotation needs no restart.

| Secret | Cluster path | Keys |
|---|---|---|
| `minio` | `/var/app-secret/app-backend/minio` | `access_key`, `secret_key` |
| `lakefs` | `/var/app-secret/app-backend/lakefs` | `endpoint`, `access_key`, `secret_key` |
| `labelstudio` | `/var/app-secret/app-backend/labelstudio` | `url`, `refresh_token` |
| `zitadel` | `/var/app-secret/app-backend/zitadel` | `USER_ID`, `KEY_ID`, `PRIVATE_KEY` |
| `zitadel_oidc` | `/var/app-secret/app-backend/zitadel-oidc` | `project_id` (+ client id and secret) |

In the cluster the Vault Agent Injector renders one file per key under `app-secret/app-backend/*`
(`deployment.yaml` template). Locally `scripts/secrets-init.sh` and the bootstrap scripts write the same
shapes to `.local/secrets/` (gitignored); compose mounts that directory at the cluster path.

The one thing still read from Vault's API is the **dynamic Postgres credential**
(`database/creds/core-app`), using the token file at `vault.token_file` (`/vault/secrets/token`).

## Other expectations of the cluster

- Probes are `tcpSocket` today. `/health` (liveness) and `/health/deps` exist; the auth plan adds `/ready`.
- Egress used: Postgres, Vault, OTel gateway, Ray dashboard, lakeFS, MLflow, Label Studio, Zitadel.
- Migrations: `core-api/migrations/*.sql`, applied by hand as the `postgres` superuser (README), not by the app or an init container.
