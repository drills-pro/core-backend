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
| `prod.yaml` | `APP_ENV=prod` (the cluster): all of its hostnames, the Zitadel issuer, in-cluster URL (signing keys are fetched there, over the `allow-traefik-and-core-api` policy) and project id |
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
| `staging` | `/var/app-secret/app-backend/staging` | `endpoint`, `access_key`, `secret_key` |
| `ingest` | `/var/app-secret/app-backend/ingest` | `endpoint`, `access_key`, `secret_key` (staging bucket only; see Datasets) |
| `zitadel` | `/var/app-secret/app-backend/zitadel` | `USER_ID`, `KEY_ID`, `PRIVATE_KEY` |

In the cluster the Vault Agent Injector renders one file per key under `app-secret/app-backend/*`
(k8s `apps/_components/vault-agent`, derived from the ServiceAccount `app-backend`). Locally `scripts/secrets-init.sh` and the bootstrap scripts write the same
shapes to `.local/secrets/` (gitignored); compose mounts that directory at the cluster path.

core-api makes no Vault API calls: the Vault agent sidecar renews the Postgres lease and rewrites
`/vault/secrets/db` when it has to issue a new user. The k8s side keeps `agent-inject-token` only until
this image is deployed; it can be removed after.

## Datasets: staging store and lakeFS (data plane)

Only `app/integrations/data_plane/` touches lakeFS, the staging bucket and MLflow. Before this image is
deployed to prod, the k8s side needs (describe, apply by hand; `/health/deps` reports `secrets` as an error
until the `staging` file exists):

- Seed the `staging` secret (S3 `endpoint`, `access_key`, `secret_key`) in `bootstrap/04-vault-seed-secrets.sh`
  so the injector renders `staging`. These credentials must read, write and delete in **both**
  `data_plane.staging_bucket` and `data_plane.lakefs_blockstore_bucket`: `delete_dataset_repo` purges the lakeFS
  storage namespace with them (lakeFS deletes only the repository record). 
- Create those two buckets, and allow browsers to PUT to the staging bucket (CORS).
- Set in `prod.yaml` once the hostnames exist: `public.staging_endpoint` (the address browsers reach the
  staging store at; the default `http://localhost:9000` is local MinIO only) and `public.lakefs_endpoint`.
- Seed a second secret, `ingest`, for the Ray ingest job: read, write and list on `data_plane.staging_bucket` only, never the lakeFS blockstore bucket (the job parses hostile input and its env is visible in the Ray job API). Per-import prefix scoping needs STS (plan risk). A matching k8s change is needed (`k8s-contract`).
- Egress: core-api to the staging store (if not lakeFS's own S3), Ray to the staging store.
- The ingest job (`core-api/ingest_job/ingest.py`, copied into the core-api image only so core-api can ship it to Ray) runs in the **Ray image**, not core-api's: that image must have Python >= 3.11, `pyarrow`, `Pillow` and `boto3` (checked against core-backend-ray-head; neither is in core-api's pyproject). The Dockerfile `COPY ingest_job` adds a file only: no port, probe, config key or secret change, so no k8s change for it.

## Other expectations of the cluster

- Probes are `tcpSocket` today. `/health` (liveness) and `/health/deps` exist; the auth plan adds `/ready`.
- Postgres is the per-app CNPG cluster `pg-backend` (host in `prod.yaml`); Vault's `core-app` role must point at the same cluster or logins fail with "password authentication failed".
- Egress used: Postgres, OTel gateway (the Vault agent sidecar, not core-api, talks to Vault), Ray dashboard, lakeFS, MLflow, Zitadel.
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

**Roles.** The Job logs in to the `backend` database as `backend_admin`, the role k8s already provisions
for this app (CNPG managed role in `apps/backend/db/cluster.yaml`: LOGIN, CREATEROLE, owner of the
database; Vault static role `backend-admin`). Not a superuser, no `BYPASSRLS`. It owns every object and
`schema_migrations`. `core_app`, the runtime group role, is created by the cluster (`bootstrap/18-pg-split.sh`),
owns nothing, and the migrations never create or alter roles.

**Rules for a migration.**
- No default privileges: each new table, sequence and function `GRANT`s what `core_app` needs in the same
  file. Never grant on `schema_migrations`. `core_app` has `USAGE` on `public`, not `CREATE`.
- Tables `ENABLE ROW LEVEL SECURITY`, not `FORCE`: the owner skips the policies, which is how `SECURITY
  DEFINER` functions read across tenants. Each of those sets `search_path = public, pg_temp`, filters
  rows itself, and is `REVOKE`d from `PUBLIC`.
- Nothing superuser-only (`CREATE EXTENSION`, ...), no `CREATE ROLE`.
- `scripts/migrations-check.sh` proves all of this from an empty PG18 with these roles
  (`compose/postgres/core-api.sql`), including that `core_app` sees one tenant while `SECURITY DEFINER` sees all.
  CI runs it before building the image.

**k8s side:** the Job's credential becomes `database/static-creds/backend-admin` instead of
`database/creds/core-migrator` (template in `apps/backend/migrate/config/kustomization.yaml`, policy in
`apps/platform-base/vault-roles/config/app-backend-migrate.hcl`); the "Migrator role" steps in
`docs/operations.md` are no longer needed. Then enable `backend-migrate` in `apps/backend/flux.yaml`. A
new migration needs nothing in k8s.

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

## Worker mode and compute (RayJob)

One image, two commands. The API (`uvicorn app.main:app`) serves HTTP; the worker (`python -m app.worker`, run as
`uv run python -m app.worker` in this image) runs only the background loops of `app/background.py`: training,
deployment and import reconcilers, import expiry, dataset purge, rate-limit purge. Each loop takes a Postgres
advisory lock per sweep, so any number of replicas is safe and exactly one does each sweep; every state change is a
compare-and-set. The worker serves no HTTP and needs no Service.

Both knobs are config (`app/core/settings.py`), overridable by env var without a new image:

| Knob | Default | Meaning |
|---|---|---|
| `background.pollers_in_api` | `true` | `false` stops the API from running the loops. Set `CORE_BACKGROUND__POLLERS_IN_API=false` on the API Deployment **after** the worker is running |
| `compute.backend` | `ray_dashboard` | `rayjob` submits a KubeRay `RayJob` CR and reads status from it; `ray_dashboard` is the Ray HTTP job API as today |
| `compute.rayjob.namespace` | `ray` | where the CRs are created |
| `compute.rayjob.cluster_name` | `""` | a warm shared `RayCluster` to run jobs on (`clusterSelector`), good for small ingest; empty = one ephemeral cluster per job from `compute.rayjob.cluster_spec` (training) |
| `compute.rayjob.ttl_after_finished_seconds` | 86400 | how long a finished CR (and its status) stays; must exceed the longest reconcile gap |

Rollout order: deploy the worker, then flip `pollers_in_api` off on the API. In between both run the loops, which is
harmless. Flip `compute.backend` to `rayjob` only after the RBAC below exists; jobs already submitted keep their ids,
so drain in-flight ones first (a job id from one backend is unknown to the other).

**k8s side (not done; describe, apply by hand):**

- **Worker Deployment** `core-worker` in the backend namespace: same image digest as the API, `command: ["uv", "run", "python", "-m", "app.worker"]`, 2 replicas (leader per loop, so both are active), same ServiceAccount (`app-backend`) and Vault agent annotations as the API (it reads the `db`, `lakefs`, `staging`, `ingest` and `zitadel` files), same ConfigMap/env. No Service, no Ingress, no probes on a port; use an `exec` liveness probe if one is wanted, otherwise a crash restarts the pod. Resources like the API's. PodDisruptionBudget `maxUnavailable: 1`.
- **RBAC**, in the Ray namespace (`compute.rayjob.namespace`): a Role for `core-worker`'s and the API's ServiceAccount granting `rayjobs` in `ray.io`: `create`, `get`, `list`, `watch`, `delete`. The API needs `create` and `delete` (submit, cancel); the status readers (worker, and the API for `GET` of a single run) need `get`. `watch` is for a later event-driven reconciler and is not used yet. Nothing on pods, secrets or RayClusters.
- **ServiceAccount token**: the default automount is what `app/integrations/compute.py` uses (`/var/run/secrets/kubernetes.io/serviceaccount`). If the namespace sets `automountServiceAccountToken: false`, turn it on for these two Deployments.
- **NetworkPolicy**: egress from the backend namespace to the Kubernetes API (`kubernetes.default.svc`, 443) for the API and worker pods; the worker keeps the API's egress to Postgres, lakeFS, MLflow, Zitadel and the staging store; it no longer needs the Ray dashboard once `compute.backend: rayjob` is on (training log reads still use it until logs move off the dashboard, see below). Ingress to the worker: none. `mlflow` and `lakefs` policies (I6) allow `backend` and `core-worker` pods.
- **KubeRay**: the operator must be at a version whose RayJob supports `clusterSelector`, `entrypointNumCpus`/`entrypointMemory`, `runtimeEnvYAML` and `activeDeadlineSeconds`; for a warm ingest cluster, an always-on `RayCluster` (CPU only) and its name in `compute.rayjob.cluster_name`; for per-run training, the `rayClusterSpec` (images, GPU worker group) goes in `prod.yaml` as `compute.rayjob.cluster_spec`. The ingest image requirements above apply to the cluster that runs it.
- **Kueue** (later, not decided yet): add `kueue.x-k8s.io/queue-name` to the CR labels and a LocalQueue per tier; no core-api change beyond one label.

**Known gaps (follow-ups, not in this change):** training job logs (`ray_client.get_logs`) still go to the Ray dashboard, so they only work for jobs on a cluster whose dashboard core-api can reach; a RayJob's logs need a log sink or the submitter pod's logs. Job env vars (including the `ingest` credential) sit in the CR's `runtimeEnvYAML`, readable by anyone with `get` on RayJobs; keep that verb to these ServiceAccounts. A stopped job's CR is deleted, so its status afterwards is a 404, which the reconcilers already treat as unknown.
