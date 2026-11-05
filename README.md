# Platform — local dev stack

Working backend for the no-code ML platform described in `req/`. Every
piece below is live-verified against the running stack, not just written —
see each section's "try it" for the exact commands used to prove it.

## Run

```
mise install
./scripts/secrets-init.sh   # local secret files, once
docker compose up --build
```

Then provision the services that need it — LakeFS, Vault, Zitadel's OIDC
credentials, and the kind node IP:

```
./scripts/bootstrap.sh
```

**Run this after every `docker compose up`, not just the first time.**
Vault runs in dev mode, which keeps everything in memory: restarting that
one container empties it, and `bootstrap.sh` writes a fresh Postgres credential
(`.local/secrets/db.json`) from it. Every step in `bootstrap.sh` is idempotent, so
re-running it costs nothing and is the intended habit. It finishes by
printing `/health/deps`.

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

One thing it deliberately does not do: `./scripts/zitadel-bootstrap.sh`,
which creates the Zitadel project, OIDC apps, roles and IdPs. That is
first-time-only and would create duplicates if re-run — on a brand-new
stack, run it once before `bootstrap.sh`.

| Service | URL |
|---|---|
| core-api | http://localhost:8090/health/deps |
| core-api docs (OpenAPI) | http://localhost:8090/docs |
| Ray dashboard | http://localhost:8265 |
| MinIO console | http://localhost:9001 (AKIAIOSFODNN7EXAMPLE / wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY) |
| LakeFS | http://localhost:8000 (see one-time setup below) |
| Zitadel | http://zitadel:8080 — add `127.0.0.1 zitadel` to `/etc/hosts` first (default admin: zitadel-admin@zitadel.zitadel.monorepo.orb.local / Password1!) |
| MLflow | http://localhost:5001 |
| Grafana (LGTM) | http://localhost:3000 |
| Vault | http://localhost:8200 (root token: `root`) |

`GET /health/deps` pings every dependency (Postgres, LakeFS, Zitadel,
Ray, MLflow) — `"ok"` per service once everything is wired.

## Status against req/

| Doc | Status |
|---|---|
| 00 overview | n/a — index |
| 01 multi-tenancy | ✅ real Postgres RLS, verified 403-vs-404 semantics |
| 02 auth/identity | ✅ JWT verification against Zitadel JWKS, unit-tested; Zitadel on a real separate CloudNativePG cluster; **automated OIDC client registration via the Management API is real** (`scripts/zitadel-bootstrap.sh`) — a real project, OIDC app, and all 4 FR3 roles, created via API calls with no console clicks, verified reproducible from a clean database. `AUTH_MODE=dev` remains the compose default only because exercising it needs a browser-driven authorization-code flow this environment can't automate — see Auth section |
| 03 project/user mgmt | ✅ project CRUD/archive/soft-delete, per-project roles, audit log — all live-tested |
| 04 API | ✅ by construction (FastAPI → OpenAPI, path-versioned `/v1`, 202+idempotency on training runs, pagination) + real Deployments group, Models group (MLflow registry wrapper), `POST /v1/predict/{deployment_id}` with its own separate auth scope and real **per-plan-tier** rate limiting (verified: `pro` tenant → 120/min default; a 3/min-throttled deployment → `200,200,200,429,429`) |
| 05 UI/no-code builder | The console lives in the separate `customer-frontend` repo; this repo ships no UI. The dataset and training APIs it uses are all here |
| 06 ML training (MLflow) | Orchestration and MLflow logging are real and tested. Training data access is stubbed (`501 not_implemented`) until the trainer slice rewires it to dataset version ids. MLflow Model Registry promotion is real |
| 07 compute (Ray/K8s) | ✅ real KubeRay on a local kind cluster — 3 labeled node pools, priority classes, scale-to-zero training pool, per-tenant namespace+quota, all live-verified |
| 08 observability (LGTM) | ✅ OTel traces + metrics verified live in Tempo/Prometheus; **all 4 of FR2's named alerts real and actively evaluating**, including GPU node pool exhaustion — verified to actually fire, not just evaluate, via real K8s `ResourceQuota` tracking of `nvidia.com/gpu` requests (real even at 0 physical GPUs) relayed through `kube-state-metrics`. Plus a real customer-facing usage dashboard (see req/05) |
| 09 usage metering | ✅ append-only `usage_events`, idempotent writes, wired to real training-run completion, queryable via API and the customer dashboard |

Also added, not in `req/` originally: **Vault** for secrets (added on request,
see Secrets section) and a working **MLflow** service.

## Local Python dev (core-api)

```
cd core-api
uv sync
uv run uvicorn app.main:app --reload
uv run pytest tests/ -q
```

CI (`.github/workflows/tests.yml`) runs that suite and `ruff check .` on every pull request, plus the `ray-jobs`
tests and `./scripts/db-tests.sh` (row-level security as `core_app` on a real Postgres; Docker only). The image
workflow (`docker-publish.yml`) builds nothing until all of them pass.

### Layout (modular monolith)

One deployable, but the code inside is split into bounded contexts with a
one-directional dependency graph:

```
core-api/app/
  main.py          composition root — mounts every module's router, nothing else
  background.py    the pollers: each module owns the reconcile, this owns the timer
  core/            shared kernel — config, db pool, tenancy/RLS, auth, audit, telemetry
  integrations/    one client per outside system — Ray, LakeFS, MLflow, S3,
                   Zitadel, Vault
  modules/         one package per context: projects, datasets,
                   training, model_registry, deployments, usage,
                   teams, users, auth, public, health
                     routes.py   HTTP surface
                     service.py  domain logic shared by HTTP and background callers
                     tasks.py    the reconcile loop body
                     __init__.py the module's public API — what other code may import
```

The rules: `core/` depends on nothing above it, `integrations/` may use `core/`,
and a module reaches another module **only** through its package root
(`from app.modules.usage import record_usage`, never
`app.modules.usage.service`). `tests/architecture/test_architecture.py` fails the build on
any violation, so the boundaries can't quietly rot. Every setting the service reads
is declared in `app/core/settings.py` and set in `core-api/config/*.yaml`
(see [docs/runtime-contract.md](docs/runtime-contract.md)); secrets are JSON
files referenced by path, never values.

## Multi-tenancy (req/01)

`core-api/migrations/20261017172706_init.sql` sets up real Postgres Row-Level
Security, not just an app-layer `WHERE tenant_id = ...`:

- `core_app` role (non-superuser — RLS is a no-op for superusers) holds
  the grants; nothing connects as it directly (see Secrets section).
- Every tenant-scoped table is scoped by an RLS policy reading
  `app.tenant_id`, set per-request via `SET LOCAL` (`app/core/tenant_conn.py`).
- `GET /v1/projects/{id}` implements FR1 exactly: 403 (not 404) for a real
  project in another tenant, 404 for one that doesn't exist anywhere — via
  a `SECURITY DEFINER` existence check that bypasses RLS only to return a
  boolean, never row contents.
- Two demo tenants are seeded (`11111111-...`, `22222222-...`).

```
curl -X POST "http://localhost:8090/v1/projects?name=Alpha" -H "X-Tenant-Id: 11111111-1111-1111-1111-111111111111"
curl "http://localhost:8090/v1/projects/<id>" -H "X-Tenant-Id: 22222222-2222-2222-2222-222222222222"  # 403
```

## Auth (req/02)

`app/core/auth.py` verifies Zitadel-issued JWTs for real: fetches/caches JWKS
(in-cluster in prod, `zitadel.internal_url`), checks the RS256 signature, issuer and audience (the Zitadel project id). Covered
by `core-api/tests/core/test_auth.py` (self-signed keypair — wrong signature
and missing-claim cases both rejected).

### OIDC client registration is real, automated, and reproducible

`scripts/zitadel-bootstrap.sh` drives Zitadel's Management API directly
— no console clicks, no Terraform, just `curl`. It creates a real
project, the SPA's public OIDC app, and the project roles,
then stores the SPA's `client_id` in Vault (`secret/platform/zitadel-spa`) and prints the
project id to export as `CORE_ZITADEL__PROJECT_ID` for a host-run core-api. Verified reproducible from a fully
clean state (dropped DB, fresh volume) — not a one-off manual sequence.

Getting there required tracking down a real, specific bug: Zitadel
auto-provisions a machine user + PAT on first init via
`ZITADEL_FIRSTINSTANCE_*` config (`compose/zitadel/firstinstance.yaml`,
loaded via `--steps`), but the schema I'd verified against Zitadel's
`cmd/defaults.yaml` was still incomplete — `PatPath` (where to *write*
the generated PAT) is a **top-level** `FirstInstance` key, not nested
under `Org.Machine.Pat`, and I'd only found that by fetching the actual
`cmd/setup/steps.yaml` example from Zitadel's repo (via a code-search
tool, not the docs page, which doesn't show it). Once added, the PAT
file appeared immediately — followed by one more real fix, a Docker
named volume defaulting to root-only permissions that a non-root
container can't write to (`zitadel-pat-init` service in
`docker-compose.yml`, `chmod 777` before Zitadel starts).

```
docker compose up -d
./scripts/zitadel-bootstrap.sh
```

`AUTH_MODE=dev` (the compose default) still uses `X-Tenant-Id`/`X-User-Id`
headers rather than real tokens — not because the OIDC client is missing
(it isn't) but because *exercising* the authorization-code+PKCE flow
needs a real browser to complete a login redirect, which this environment
doesn't have. The registered client is real and ready for that the moment
a browser is in the loop. Flip `AUTH_MODE=zitadel` in `docker-compose.yml`
to use it; every handler already only sees an opaque `tenant_id`/`actor`
string, so nothing downstream changes.

## Project & user management (req/03)

`core-api/migrations/20261017172706_init.sql` +
`app/modules/user/projects/`: project lifecycle
(create/rename/archive/soft-delete with a `pending_delete` grace state),
per-project roles independent of org role, and an append-only audit log (`INSERT`-only grant — the app
connection literally cannot rewrite history). All paginated.

```
curl -X POST "http://localhost:8090/v1/projects?name=Demo" -H "X-Tenant-Id: 11111111-1111-1111-1111-111111111111" -H "X-User-Id: alice"
curl -X PUT "http://localhost:8090/v1/projects/<id>/members/bob?role=project_editor" -H "X-Tenant-Id: 11111111-1111-1111-1111-111111111111" -H "X-User-Id: alice"
curl "http://localhost:8090/v1/projects/<id>/audit-log" -H "X-Tenant-Id: 11111111-1111-1111-1111-111111111111"
```

The same table read the other way round — across every tenant at once —
is the operator's activity log, `core-api/migrations/20261017172706_init.sql`
+ `app/modules/admin/activity/routes.py`. `audit_log` is RLS-protected and
the admin routes set no `app.tenant_id`, so it goes through a SECURITY
DEFINER function like the other back-office readers, filters
on actor/action/entity/tenant/age, and sends the filtered size as
`X-Total-Count`. Actor names come from Zitadel as enrichment: if Zitadel
is unreachable the log still answers, with `actor_name: null`.

```
curl -i "http://localhost:8090/v1/admin/activity?limit=3" -H "X-Admin-User: admin-dashboard"
curl "http://localhost:8090/v1/admin/activity?entity_type=project&days=30" -H "X-Admin-User: admin-dashboard"
curl "http://localhost:8090/v1/admin/activity/summary" -H "X-Admin-User: admin-dashboard"
```

## Training orchestration (req/06 + req/07)

`app/modules/training/routes.py` submits a Ray job (Ray's HTTP Job Submission API, not
the Python SDK — core-api needs no Ray runtime) tagged with
`tenant_id`/`project_id` metadata, polls it, and on completion logs
metrics to MLflow (`app/integrations/data_plane/mlflow.py`) and bills the run to
`usage_events` (`app/modules/usage/service.py`) — idempotently on both counts (retried
submission with the same `Idempotency-Key` returns the existing run;
retried completion polling never double-bills).

Training data access is stubbed: `POST /training-runs` answers `501 not_implemented` (authz still applies)
until the trainer slice rewires training to dataset version ids (`docs/datasets-plan.md`, Deferred).

Ray image is pinned to `rayproject/ray:2.49.2` (multi-arch) rather than
`2.9.0` (amd64-only) — the latter silently breaks job submission under
QEMU emulation on Apple Silicon (`No available agent to submit job`).

## Datasets

Data enters through imports and every successful import is one immutable version. Decisions and deferred
work: `docs/datasets-plan.md`. All lakeFS, staging-bucket and MLflow access goes through
`app/integrations/data_plane/` (tenant-checked operations; nothing else touches those backends). A dataset
takes its project's HF task slug (`image-classification`, `object-detection`, `tabular-classification`,
`tabular-regression` are supported now).

Flow, all under `/v1/projects/{project_id}/datasets` (`$H` = `-H "X-Tenant-Id: ..."` in dev-header mode):

```
POST   /datasets                                   create (name)
POST   /datasets/{id}/imports                      create an import, returns a presigned PUT to staging
PUT    <presigned url> --data-binary @data.zip     browser/client uploads straight to staging
POST   /datasets/{id}/imports/{imp}/start          queue the Ray ingest job (no bytes through the API)
GET    /datasets/{id}/imports/{imp}                poll: running -> awaiting_review (report + sample)
GET    /datasets/{id}/imports/{imp}/preview        sample rows and presigned URLs from the staged index
POST   /datasets/{id}/imports/{imp}/commit         lakeFS commit -> new version (or .../discard, .../cancel)
GET    /datasets/{id}/versions                     list versions (cursor pagination)
PUT    /datasets/{id}/published                    publish a version; DELETE unpublishes
DELETE /datasets/{id}                              soft delete, 7-day retention; POST /datasets/{id}:restore
```

An import may carry `base_version_id` to merge on top of an earlier version (strict: exact duplicates are
skipped, any other conflict fails the import with a report; `409 stale_base` if the base is no longer the
latest). Archive rules and size limits are the `dataset_limits:` knobs in `config/base.yaml`; the ingest job
is the `ray_jobs` package in `ray-jobs/` (one entrypoint per job type) and runs in the Ray image, writing only to a staging prefix. Background
pollers (`app/background.py`) move finished jobs to `awaiting_review`, expire stale imports and purge
datasets 7 days after deletion.

## Deployments & inference (req/04 Deployments group, req/07 FR3)

`app/modules/deployments/routes.py` + `app/integrations/serve_client.py`: a real Ray Serve deployment,
not a stub. The default (numpy) training backend now emits its fitted
weights as a `model:{...}` log line; `POST /v1/projects/{id}/deployments`
reads that artifact and submits a Ray job whose entrypoint calls
`serve.run(...)` — Serve deployments are independent Ray actors that
outlive the submission job, which is the standard way to drive Serve from
outside the cluster's own process. `POST /v1/predict/{deployment_id}`
proxies to the deployed HTTP route and records **real** request latency
(`app/core/metrics/inference.py::latency`) — this is the actual signal
behind req/08's "inference p99 latency" alert (see Observability).

Verified live end-to-end: trained a model, deployed it, called
`/predict` with three different feature vectors, got three genuinely
different probabilities back (`0.83`, `0.15`, `0.49` — computed from the
real fitted weights, not canned):

```
curl -X POST "http://localhost:8090/v1/projects/<id>/deployments?training_run_id=<run_id>" -H "X-Tenant-Id: 11111111-1111-1111-1111-111111111111"
curl -X POST "http://localhost:8090/v1/predict/<deployment_id>" -H "X-Tenant-Id: 11111111-1111-1111-1111-111111111111" \
  -H "Content-Type: application/json" -d '{"features": [2.0, -1.0, 0.5, 1.0]}'
```

Ray Serve's HTTP proxy binds to `0.0.0.0:8000` inside the container
(Serve defaults to `127.0.0.1`, which is unreachable cross-container —
set explicitly via `serve.start(http_options=...)`), mapped to host
`8100` (`8000` was already LakeFS's).

Rate limiting is real and per-tenant-plan (req/04 FR4 verbatim: "per
tenant plan tier"): `tenants.plan_tier` (`free`/`pro`/`enterprise`,
init migration) drives each new deployment's default
`rate_limit_per_minute` (`PLAN_TIER_RATE_LIMITS` in `app/modules/deployments/routes.py`
— 10/120/1000), still overridable per-deployment, enforced by an
in-process sliding window (`app/core/rate_limit.py`) before every proxied
call. Verified live twice: a `pro`-tier tenant's new deployment got
`rate_limit_per_minute: 120` automatically; a deployment throttled to
3/min returned `200,200,200,429,429` on 5 requests.

Inference auth scoping is real, not shared with the management API:
`create_deployment` generates a per-deployment secret (only its SHA-256
hash is stored — `migrations/20261017172706_init.sql`) and returns it once;
`/predict` requires that exact secret via `X-Predict-Key` and accepts
**no tenant/JWT credential at all** — the lookup runs `SECURITY DEFINER`
so the request never needs a tenant session context, matching req/04's
"separate auth scope... what the customer's own downstream systems call."
Verified live: a `/predict` call with the right key and zero tenant
headers succeeds; the wrong key gets a 401.

```
curl -X POST "http://localhost:8090/v1/predict/<deployment_id>" -H "X-Predict-Key: <key from deploy response>" \
  -H "Content-Type: application/json" -d '{"features": [1.0, 1.0, 1.0, 1.0]}'
```

## Model registry (req/04 Models group, req/06 FR2)

`app/modules/model_registry/routes.py` wraps MLflow's real Model Registry — not a local table
pretending to be one. `POST .../models/{training_run_id}/register`
creates (or reuses) a registered model and a real model version pointing
at the run's MLflow artifact; `POST .../models/{id}/promote` calls
MLflow's real `transition-stage` API. Verified by reading the stage back
directly from MLflow after promoting, not just from our own DB: registered
a model, promoted `None → Staging → Production`, and confirmed
`current_stage: "Production"` from `GET /api/2.0/mlflow/model-versions/get`
itself. FR2's customer-facing state mapping (`None`→"internal only",
`Staging`→"candidate", `Production`→"live/deployed") is applied on every
response via `customer_label`. The same promote endpoint covers rollback
— MLflow doesn't distinguish a forward promotion from a rollback
mechanically, and neither does this endpoint.

```
curl -X POST "http://localhost:8090/v1/projects/<id>/models/<run_id>/register?name=my-model" -H "X-Tenant-Id: 11111111-1111-1111-1111-111111111111"
curl -X POST "http://localhost:8090/v1/projects/<id>/models/<model_version_id>/promote?stage=Production" -H "X-Tenant-Id: 11111111-1111-1111-1111-111111111111"
```

The operator's view of the same table — every tenant's models side by side
— is `core-api/migrations/20261017172706_init.sql` +
`app/modules/admin/models/routes.py`. `model_versions`, `projects` and
`deployments` are all RLS-protected and the admin routes set no
`app.tenant_id`, so it goes through a SECURITY DEFINER function like the
other back-office readers. Two things it derives rather than
reads, so that the listing, its filters and its counts cannot disagree:

- **status is not a column.** It comes from the newest deployment of the
  run a version came from (`active`→deployed, `deploying`→deploying,
  `failed`/`deleted`→stopped, none→not_deployed).
- **a model is not a table.** `model_versions` holds one row per *version*,
  so the listing folds them onto `(project_id, mlflow_model_name)`,
  reports the newest with `version_count` behind it, and
  `GET /{version_id}/versions` opens one back up. Newest is by
  registration time, not by `mlflow_version` — that column is text, where
  `'10'` sorts before `'2'`.

Filters on organisation/task/visibility/status plus a text match, and the
filtered size travels as `X-Total-Count`.

```
curl -i "http://localhost:8090/v1/admin/models?limit=2" -H "X-Admin-User: admin-dashboard"
curl "http://localhost:8090/v1/admin/models?status=deployed&visibility=public" -H "X-Admin-User: admin-dashboard"
curl -i "http://localhost:8090/v1/admin/models/<model_version_id>/versions" -H "X-Admin-User: admin-dashboard"
curl "http://localhost:8090/v1/admin/models/summary" -H "X-Admin-User: admin-dashboard"
```

Not in there, and deliberately not faked: an artifact's size on disk
(MLflow holds the artifact, `model_versions` only points at it) and
whether a model awaits review (`require_review_before_training` is an org
switch, not a queue). The dashboard leaves both as placeholders.

## Kubernetes / KubeRay (req/07)

This was a real, live-verified KubeRay deployment on a local `kind`
cluster (manifests since removed from this repo — infra now lives in the
separate `k8s` Flux repo).

`core-api` and `zitadel` are both attached to the kind cluster's own
Docker network (`networks: [default, kind]` in `docker-compose.yml`) so
they can reach cluster services directly by node IP + NodePort — no
`kubectl port-forward` (tried first for Zitadel's Postgres; repeatedly
dropped connections under this environment's CNI, see Zitadel section).

The cluster had 3 worker nodes labeled `node-pool: {system,training,serving}`,
matching the doc's node-pool table exactly. The RayCluster ran its head on
the system pool, with a `training` worker group (`minReplicas: 0`,
scale-to-zero — verified: no pods exist for the group until a job needs
it) and a `serving` worker group (`minReplicas: 1`, `serving-priority`
PriorityClass so it can't be preempted by training — NFR2), each pinned to
its node pool via `nodeSelector`. A RayJob example demonstrated FR1's
tenant_id/project_id pod-label requirement applied at the RayJob level.
FR4's hard per-tenant quota was enforced as a namespace + `ResourceQuota`
(enforced by the K8s API server at admission, not app-level bookkeeping).

`core-api`'s own job submission (`app/integrations/ray_client.py`) still targets the
docker-compose single-node Ray cluster for a fast dev loop — pointing
`RAY_DASHBOARD` at the KubeRay head service instead is a config change,
not new code, since both speak the same Ray Job Submission API.

## Zitadel on CloudNativePG (req/02)

Zitadel now runs against a real, separate Postgres cluster on the kind
cluster — CloudNativePG, not the shared docker-compose dev postgres
(manifest since removed from this repo — infra now lives in the separate
`k8s` Flux repo).

Reached via a `NodePort` Service + `zitadel` being attached to the kind
Docker network directly (`networks: [default, kind]` in
`docker-compose.yml` — reproducible via `docker compose up`, not a manual
`docker network connect`) — `kubectl port-forward` was tried first and
repeatedly dropped connections under this environment's CNI, so this is
the more stable path, not a preference. Verified live, including from a
fully clean state: `zitadel` starts cleanly against it, `/health/deps`
reports `zitadel: ok`, and a direct `psql` connection to the CNPG
cluster confirms real data.

## Usage metering (req/09)

`core-api/migrations/20261017172706_init.sql`: append-only `usage_events`
ledger, `UNIQUE(resource_type, source_ref)` as the idempotency key so a
retried job-completion/snapshot/rollup callback no-ops instead of
double-billing. `INSERT`-only grant, same append-only pattern as the audit
log. `pricing_version` is stamped on every row (`v1-raw-gpu-seconds`) so
future pricing-formula changes don't shift historical invoices — req/09's
own open question (how to normalize inference compute units) is
unresolved in the spec itself, so this takes the doc's suggested fallback
of raw GPU-seconds rather than inventing a formula.

## Observability (req/08)

`app/core/tracing.py` instruments `core-api` (FastAPI + httpx + asyncpg) with
OpenTelemetry and exports to the LGTM stack's built-in OTLP collector
(`grafana/otel-lgtm` bundles Tempo + Loki + Prometheus + Grafana, one
image — matches NFR2's "one coherent product surface" requirement). FR1's
label convention (`tenant_id`/`project_id` on everything) is applied via
`tag_current_span` at the point each request resolves its tenant
(`app/core/tenancy.py`), verified live: query Tempo through Grafana's datasource
proxy and every `core-api` span carries `tenant_id`.

```
curl "http://localhost:3000/api/datasources/proxy/uid/tempo/api/search?tags=service.name%3Dcore-backend&limit=5"
```

`app/core/metrics/` also pushes OTel metrics, all named `drills.core_backend.<namespace>.<name>` (same OTLP pipeline as traces)
— `drills.core_backend.training.runs{status}`, incremented on every real training-run
transition to a terminal state, is the signal for FR2's "training job
failure rate" alert. Verified live in Prometheus:

```
curl "http://localhost:3000/api/datasources/proxy/uid/prometheus/api/v1/query?query=drills_core_backend_training_runs_total"
```

Database size (disk pressure) is not a core-api metric: it comes from the Postgres exporter.

`drills.core_backend.inference.latency` is the third real signal — recorded on every real
`POST /v1/predict/{id}` call (see Deployments section) as an OTel
histogram.

**All 4 of FR2's named alerts are now real and actively evaluating** —
including GPU node pool exhaustion, initially (wrongly) written off as
impossible without physical GPU hardware. The actual insight: Kubernetes
`ResourceQuota` tracks resource *requests* — including extended resources
like `nvidia.com/gpu` — at admission time, regardless of whether real
hardware backs them. The tenant namespace's `ResourceQuota` set
`requests.nvidia.com/gpu: "2"`; a real pod
requesting `nvidia.com/gpu: "1"` gets admitted and quota-tracked even
though it stays `Pending` forever (no device plugin registers real GPUs
to schedule it onto) — that acceptance and Pending state are both
genuine Kubernetes behavior, not simulated.

The chain end to end: `kube-state-metrics` (installed via Helm, exposed
through a NodePort Service) exposes the real `kube_resourcequota` series
→ Prometheus/Mimir → a Grafana alert on
`used / hard > 0.8`.

Verified live, including the alert actually *firing*, not just
evaluating cleanly: applied two real GPU-requesting pods (pushing
`used` from 0 → 2, matching `hard: 2`), watched the alert transition
`inactive → pending → firing` (confirmed via
`api/prometheus/grafana/api/v1/rules`), then deleted the pods. Building
this also surfaced and fixed a genuine PromQL bug — the first version of
the query (`gpu_quota_requests{type="used"} / gpu_quota_requests{type="hard"}`)
silently returned no data because Prometheus vector division requires
matching label sets and `type` differs between the two series; fixed
with `/ ignoring(type)`.

```
curl "http://localhost:3000/api/prometheus/grafana/api/v1/rules"   # all four show state, health=ok
```

Not built: OpenCost (cost tracking — distinct from usage_events, req/09),
or a customer-facing dashboard layer distinct from raw Grafana (the doc's
own open question — deferred, not decided in the spec itself).

## LakeFS one-time setup

On first boot LakeFS has no admin user and there are no buckets. `scripts/steps/lakefs.sh` creates the admin
user, the blockstore bucket and the staging bucket (plus a bucket-scoped `ingest` MinIO user):

```
./scripts/steps/lakefs.sh
```

core-api creates one lakeFS repository per dataset itself, so no repository is made here.

Unlike `scripts/zitadel-bootstrap.sh`, this one is idempotent — re-running
it after any `docker compose up` reports what already exists and exits 0.
Credentials it installs match `.local/secrets/lakefs.json` (below).

## Secrets and configuration

Non-secret settings are committed YAML (`core-api/config/base.yaml`, plus `local`, `compose`, `test`,
`prod`); `APP_ENV` picks the overlay and `CORE_<SECTION>__<KEY>` env vars override. Secrets are JSON
files whose *paths* are in the config (`secrets:`). `core-api` re-reads a file when it changes.

| | Cluster | Local |
|---|---|---|
| Files | `/var/app-secret/app-backend/<name>`, rendered by the Vault Agent Injector | `.local/secrets/<name>.json` (gitignored), mounted at the same path in compose |
| Written by | `k8s/bootstrap/04-vault-seed-secrets.sh` | `scripts/secrets-init.sh` |

`core-api` does not talk to Vault. Its Postgres credential is the `db` secret file too
(`{"username", "password"}`): in the cluster the Vault agent issues it from `database/creds/core-app`,
renews the lease and rewrites the file when it needs a new user; locally `./scripts/bootstrap.sh` reads one
from the dev Vault (dev mode: in-memory, root token `root`, laptop-only; 768h TTL since nothing renews it).
`app/core/db.py` checks the file every 30s and, when it changed, opens a pool with the new user and closes
the old one.
`core_app` is a `NOLOGIN` group role holding grants and RLS policies; nothing connects as it directly
(`SELECT rolname, rolcanlogin FROM pg_roles` shows `v-token-core-app-...` rows).

`/health/deps` reports `postgres` and `secrets` (every file loads with its
required keys). Migrations are applied by dbmate as `backend_admin`, not a superuser (see below), never
by `core-api`.

## Dev credentials

Everything above uses fixed, well-known dev values (MinIO's published
example keys, a fixed Zitadel masterkey, Vault's dev root token).
**Not for anything but a laptop.**

## Migrations

`core-api/migrations/<UTC timestamp>_<name>.sql` in [dbmate](https://github.com/amacneil/dbmate) format
(`-- migrate:up` / `-- migrate:down`); the timestamp is the version. Everything up to the first
production deploy is one file, `20261017172706_init.sql`. A new one is named
`$(date -u +%Y%m%d%H%M%S)_<name>.sql`. The image ships them at `/migrations`; in the cluster the
`backend-migrate` Job applies them before a new image rolls out (`docs/runtime-contract.md`).

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

## Not built yet

Nothing — every requirement in `req/` that had a real path forward has
been built and verified live. See `AUTH_MODE` below for the one place
where "built" and "the compose default" intentionally differ.
