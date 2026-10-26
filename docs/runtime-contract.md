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
- The ingest job is the `ray_jobs` package (`ray-jobs/`, its own `pyproject.toml`), one entrypoint per job type: `python -m ray_jobs.entrypoints.<upload_images|upload_csv|huggingface>`. It runs in the **Ray image**, not core-api's: that image must have Python >= 3.11, `pyarrow`, `Pillow` and `boto3`. The cluster's image is the stock `rayproject/ray:2.60.0…-py311-cu128`, which does not carry them, so `ray-jobs/Dockerfile` builds the same base plus these dependencies (workflow `.github/workflows/ray-jobs-image.yml` pushes `ghcr.io/drills-pro/core-backend/ray-jobs:<sha>`). Put its digest in k8s `apps/ray/app/raycluster.yaml` for head and worker by hand (that restarts the head and the GPU worker, and the `ray` namespace needs an `imagePullSecret` for ghcr). Job code still ships as the zip; only dependencies are baked in.
- **What it reads:** the job spec, see [job-spec.md](job-spec.md), as one JSON argv.
- **How it reaches Ray:** the core-api image carries `ray-jobs/ray_jobs` at `/ray-jobs/ray_jobs`. On `submit_ingest` (the datasets module builds the spec, `data_plane` submits it) core-api zips it deterministically, uploads it once per code version to the staging bucket as `jobs/ray-jobs-<sha256>.zip` (with its own `staging` credential), and puts a presigned GET in the Ray job's entrypoint. A fixed few-line `python -c` bootstrap downloads and unpacks the zip into a temp directory and runs the entrypoint module; the params are plain argv (no base64 script, no argv size problem). The job gets no new credential for this. When `ray_client.submit_raw_job` can take a `runtime_env`, the same URL goes in `working_dir` and the bootstrap goes away; baking the package into the Ray image (`pip install` of `ray-jobs/`) keeps the same module names and removes the download.
- **k8s:** for the code move, none. For the data plane itself see below and k8s `docs/operations.md` ("Dataset data plane").
- **Prod config:** `prod.yaml` sets `data_plane.staging_bucket: drills-pro-staging` (its own GCS bucket), `lakefs_blockstore_bucket: drills-pro` and `public.staging_endpoint: https://storage.googleapis.com`. `public.lakefs_endpoint` is still unset (falls back to the localhost default) until lakeFS's S3 gateway has a host without the oauth2-proxy in front.
- **Secrets:** `app-secret/app-backend/{staging,ingest,lakefs}` in Vault (k8s `bootstrap/19-data-plane-secrets.sh`): `staging` = the lakeFS HMAC key, `ingest` = an HMAC key limited to the staging bucket, `lakefs` = lakeFS user credentials.
- **Code move:** The image build context is now the repo root (`.github/workflows/docker-publish.yml` passes `context: .` and `file: ./core-api/Dockerfile`; `ray-jobs/**` also triggers the build), so no manifest, port, probe or config key changes. Two things to check on the cluster side: the `ingest` credential must be allowed to **read** `jobs/` in the staging bucket (it already has bucket-wide read, write and list; a future per-prefix scoping of `ingest` must keep `jobs/*` readable), and no bucket lifecycle rule may expire `jobs/`.

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

**k8s side (not done; describe, apply by hand).** Checked against the `k8s` repo as it is today (backend in
`apps/backend/app/`, namespace `platform`, ServiceAccount `app-backend`, KubeRay operator 1.7.1, one `RayCluster` named
`ray` in namespace `ray`):

- **Worker Deployment** `core-worker`, a second Deployment in `apps/backend/app/` (add it to that `kustomization.yaml`). Everything
  else comes from the components that kustomization already applies to every Deployment in it: `../image` pins the same
  digest, `vault-agent` and `vault-db-creds` inject the same secrets and DB creds. Use `serviceAccountName: app-backend`, so
  the Vault role (derived from the ServiceAccount name) needs no change. Differences from `backend`: label `app: core-worker`
  (not `backend`, or the `backend` Service would select it), `command: ["uv", "run", "python", "-m", "app.worker"]`, no
  `ports`, no readiness probe, no Service or Ingress, `replicas: 2`, the same OTel env vars and `imagePullSecrets`. Add a PodDisruptionBudget
  (`maxUnavailable: 1`) when the second replica exists.
- **Ordering** is already right: `flux.yaml` runs `backend-migrate` before `backend`, so the worker and the API roll out after the same
  migration. Roll them from the same digest and keep migrations expand-then-contract, because the two Deployments briefly run different pods
  of one release.
- **RBAC**: a `Role` in namespace `ray` and a `RoleBinding` to the ServiceAccount `app-backend` in `platform`. `rayjobs` in
  `ray.io`: `create`, `get`, `list`, `watch`, `delete`. Nothing on pods, secrets or RayClusters. The API pods need `create`/`delete`
  (submit, cancel) and `get`; `watch` is for a later event-driven reconciler and is unused today. `app/integrations/compute.py` reads the pod's
  default ServiceAccount token, so do not set `automountServiceAccountToken: false`.
- **NetworkPolicy**: the repo only has ingress policies (no egress deny), so no egress rule is needed for the Kubernetes API.
  Ingress changes in namespace `ray`: `allow-backend-to-ray-head` selects pods `app: backend` for port 8265; add `app: core-worker`
  to its `from` list for as long as `compute.backend` is `ray_dashboard` or training logs are read from the dashboard (the worker polls
  it). RayJob submitter pods run in `ray` and are covered by `allow-intra-ray`. The worker serves nothing, so `platform` needs no new ingress rule.
  `mlflow`/`lakefs` ingress (I6) must admit the worker if those policies are tightened to named callers.
- **RayJob on the existing cluster**: `compute.rayjob.cluster_name: ray` in `prod.yaml` runs jobs on the warm `RayCluster` through
  `clusterSelector` (no cold start; suits ingest). Training per run needs `compute.rayjob.cluster_spec` with the head and GPU worker group
  of `apps/ray/app/raycluster.yaml` (same pinned Ray image, `runtimeClassName: nvidia`, the `isel-5090` node selector); do that before moving training
  to ephemeral clusters, since a second cluster cannot get the GPU while `ray` holds it. KubeRay 1.7.1 supports every RayJob field the seam sets.
- **Kueue** (later, not decided yet): one `kueue.x-k8s.io/queue-name` label on the CR and a LocalQueue per tier.

**Known gaps (follow-ups, not in this change):** training job logs go through `compute.logs`: the dashboard backend reads them from the Ray dashboard, the `rayjob` backend reads the submitter pod's log, which needs `list` on `pods` and `get` on `pods/log` for the backend's ServiceAccount (k8s repo, unverified against a running cluster; planning gap E7). The dashboard backend enforces a job's `max_runtime_s` by wrapping the entrypoint in `timeout`. Job env vars (including the `ingest` credential) sit in the CR's `runtimeEnvYAML`, readable by anyone with `get` on RayJobs; keep that verb to these ServiceAccounts. A stopped job's CR is deleted, so its status afterwards is a 404, which the reconcilers already treat as unknown.
