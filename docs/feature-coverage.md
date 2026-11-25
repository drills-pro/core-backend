# Feature coverage

Which designed capabilities of training, the model registry, deployments, serving, the predict API, jobs and datasets are built, and which are tested.
Generated from [feature-coverage.yaml](feature-coverage.yaml); do not edit by hand. Regenerate with:

```bash
core-api/.venv/bin/python scripts/feature-coverage-report.py
```

`core-api/tests/architecture/test_feature_coverage.py` keeps the manifest honest: every cited file, test function and route exists, every in-scope route is recorded, and a built feature names a test.
Status: built = works and is wired; partial = works with a stated gap; missing = designed, not built; deferred = the design itself schedules it later.

## Totals

| Area | built | partial | missing | deferred | total |
|---|---|---|---|---|---|
| training | 24 | 8 | 0 | 5 | 37 |
| registry | 13 | 3 | 0 | 0 | 16 |
| deployment | 15 | 2 | 1 | 2 | 20 |
| serving | 11 | 2 | 0 | 2 | 15 |
| predict | 16 | 7 | 0 | 3 | 26 |
| jobs | 13 | 8 | 3 | 3 | 27 |
| datasets | 25 | 3 | 0 | 3 | 31 |
| **all** | 117 | 33 | 4 | 18 | 172 |

Built share of the features that are due (partial counts half, deferred excluded): 87% of 154.

## Gaps, most important first

Weight 3 blocks the product path or tenant isolation, 2 is a real gap, 1 is polish. Missing outranks partial, partial outranks deferred.

| Weight | Status | Id | Area | Feature | Gap |
|---|---|---|---|---|---|
| 3 | partial | JOB-22 | jobs | Suspending a tenant stops what is already running (jobs, deployments, sweeps) | Predict answers 503 for a suspended tenant (PRD-06), but suspending cancels no job and stops no deployment, and the inflight_*_tenant_ids sweeps do not skip suspended tenants (read from the SQL, not run). |
| 3 | deferred | JOB-20 | jobs | Per-job prefix-scoped storage credential (Credential Access Boundary or per-import STS) | Deferred by decision 2026-11-12 until the sprint 4 credential broker; the accepted risk E6 in planning/07 stands. |
| 3 | deferred | TRN-20 | training | Free-tier GPU-minute admission, 402 quota_exhausted, spend cap, minutes estimate | Only concurrency is limited; nothing bounds GPU minutes, and no run is refused for exhausted allowance. Deferred with the entitlements model (planning/20 sprints 5 to 7); nothing to enforce until plans exist. |
| 2 | missing | DEP-16 | deployment | Legacy per-deployment Serve app started by a Ray job (deprecated serving mode) | Retired on purpose (migration drop_legacy_deployments_active); every deployment, a leftover serving = legacy row included, is answered by the shared predictor apps. |
| 2 | missing | JOB-15 | jobs | Attempt fencing - attempt in token, prefixes and events; stale attempts rejected | Not built for jobs (prediction attempts are fenced, see PRD-13); a zombie training run could still write its report. |
| 2 | partial | DEP-13 | deployment | Redeploy a newer model onto the same endpoint (deploy_strategy main) and roll back by promoting | There is no update-to-version route; a redeploy is another create under the project setting deploy_strategy=main, and there is no audited rollback. |
| 2 | partial | JOB-03 | jobs | Envelope fields attempt and reporting (events_url, token_env) | reporting is built for train; attempt is not in the envelope. |
| 2 | partial | JOB-12 | jobs | Run-time integrity check of the package a job loads | The hash is checked when a package is registered; the job itself does not re-verify what it unpacks, and one staging key can still overwrite a package object (unverified here). |
| 2 | partial | JOB-14 | jobs | Lost-job detection - suspect on silence, confirm with the compute API, then fail and free the slot | Detection is by Ray state and a fixed deadline (LOST_JOB_GRACE_SECONDS); there are no heartbeats to go silent, so a hung job is found only at the deadline. |
| 2 | partial | JOB-23 | jobs | Per-plan limits stored as data and edited by an operator (imports, runs, archives, images, tables, predict, deployments) | Limits are counts and sizes; no GPU-minute allowance, no replica caps, no per-plan retention (planning/14). |
| 2 | partial | JOB-25 | jobs | Contract tests as a hard merge gate (job spec, report schema, trainer contract, DB isolation) | tests.yml runs core-api, ray-jobs, ray-training, ray-serving and the Postgres isolation matrix on pull requests; branch protection is not visible from the repo. |
| 2 | partial | PRD-16 | predict | Usage - one event per 2xx prediction id, none for failures, replays or retries | Rule is built on the interim record_usage path; the OTLP/outbox pipeline of planning/15 does not exist, so there is no per-key dimension or live counter. |
| 2 | partial | PRD-21 | predict | Input forms - multipart, base64 JSON, image URL with SSRF guard, decompression-bomb guard | Only JSON with image_base64 (or text) is read, header-first with a pixel limit; no multipart and no image-URL input, so no SSRF path exists to guard. |
| 2 | partial | PRD-23 | predict | Key rotation or recovery, last-used time, first-use audit, publishable-key nudges | last_used_at is written (at most once a minute) and listed; a new key can overlap the old one until it is revoked, but there is no first-use audit row or publishable-key nudge. |
| 2 | partial | REG-14 | registry | Download by signed URL and auto-generated model card | A signed-in project member downloads model.onnx or model.json through an audited stream (the session token is the credential, so no signed link is issued); an unauthenticated link for publish_artifact and the generated model card are not built. |
| 2 | partial | SRV-06 | serving | Cold-start handling (503 with Retry-After, or a bounded wait) | A first call waits for the model load inside the sync timeout and the load is logged (cold_load); an unreachable Serve is 503 without Retry-After, and there is no scale-to-zero. |
| 2 | partial | SRV-08 | serving | Serving isolation - ONNX only, model URL allowlist, no redirects, pixel limit, pool pinning | Guards inside the replica are built; per-tenant serving caps and the PriorityClass pool reservation live in the k8s repo and were not checked here. |
| 2 | partial | TRN-13 | training | Run state machine with compare-and-set transitions | Five states (submitted, running, succeeded, failed, cancelled); no queued, cancelling, retry or lost, and cancel is not a desired state the worker converges to. |
| 2 | partial | TRN-14 | training | Trainer events API (idempotent by seq, only a running run takes events) | Route is per training run with a random per-run token (sha256 at rest), not POST /internal/v1/jobs/{id}/events with a signed per-job JWT carrying attempt; no attempt fencing (see JOB-17). |
| 2 | partial | TRN-15 | training | Live progress (latest epoch and history) on the run | One event per epoch only; no heartbeats every 60 s and no metric batches every 10 s, so a long epoch looks silent. |
| 2 | partial | TRN-21 | training | GPU-seconds metering of runs (usage event per finished run, once) | Written to usage_events through record_usage; no outbox, no OLAP pipeline, no per-minute reconciliation (planning/15). |
| 2 | partial | TRN-33 | training | Training data to the trainer by presigned reads plus a staged manifest | URLs are signed once for up to 7 days (MAX_PRESIGN_SECONDS); no manifest endpoint, no refresh of expiring URLs, no per-job prefix-scoped credential (see JOB-24). |
| 2 | deferred | DEP-18 | deployment | Replica scale min/max per deployment limited by plan; scale-to-zero on the free tier | Autoscaling is per shared app from config knobs (see SRV-09); a deployment has no replica settings and nothing is scaled by plan. Deferred with the entitlements model (planning/20 sprints 5 to 7); nothing to enforce until plans exist. |
| 2 | deferred | JOB-04 | jobs | One jobs table with kind, spec, state and attempt | Deliberately per-context tables on one shared lifecycle engine (ADR-007 amended 2026-11-12). A jobs table and /operations resource wait until the per-table shape costs more than it saves. |
| 2 | deferred | TRN-28 | training | Base model served from the platform mirror, never the Hub at run time; licence recorded | The platform mirror is deferred by decision 2026-11-12 until a bucket and mirroring job exist in the k8s repo; the trainer still reads the Hub at a pinned revision. |
| 1 | missing | JOB-17 | jobs | Reconciler wake-up through LISTEN/NOTIFY | Pollers tick every 10 s (60 s for sweeps, 1 s for predictions). |
| 1 | missing | JOB-21 | jobs | credential_issuances ledger of minted credentials | Nothing mints credentials per job, so nothing is recorded. |
| 1 | partial | DAT-19 | datasets | Canonical formats - WebDataset shards (vision) and Parquet (tabular) | Images are stored as objects plus a Parquet index and tables as Parquet; no WebDataset shards, so there is nothing for a streaming loader to read yet. |
| 1 | partial | DAT-20 | datasets | Dataset statistics and health report from ingest (class balance, counts, corrupt files, duplicates) | No tiny-class or imbalance warning and no near-duplicate detection; exact duplicates are collapsed. |
| 1 | partial | DAT-31 | datasets | Cleanup of per-run training prefixes in the staging bucket | A failed purge is only logged; no sweep retries it, and no bucket lifecycle rule exists in the k8s repo. |
| 1 | partial | DEP-06 | deployment | Deployment states and their legal moves, declared once and written only through job_lifecycle.advance | Status is the observed state; there is no desired-versus-observed split and no updating or stopping state. |
| 1 | partial | JOB-10 | jobs | Job catalog - a JobType names its entrypoint module | Entry has no per-type default resources or image flavour (slim, vision, nlp, gpu); the train driver is a constant outside the enum. |
| 1 | partial | JOB-26 | jobs | Journey scripts (import, train and deploy, predict) run against the local stack | Scripts exist and were run by hand on the local stack; no CI job runs them and Schemathesis contract tests are not set up. |
| 1 | partial | PRD-07 | predict | Rate limits - plan, per key (api_keys.rate_limit_per_minute) and per deployment, 429 with Retry-After | Fixed one-minute windows in Postgres (one upsert per check, the documented ceiling); no RateLimit-Limit / -Remaining / -Reset headers. |
| 1 | partial | PRD-17 | predict | Legacy POST /v1/predict/{deployment_id} with X-Predict-Key, marked Deprecation and Sunset, counted per deployment | Phase 1 (shadow) is done; phases 2 and 3 (stop minting predict_key, remove the route, deployment_for_predict and predict_key_hash) are not. |
| 1 | partial | PRD-24 | predict | Authenticate, rate-limit and size-check before reading the body; stream with limits on the byte stream | The key is checked first and the declared length is refused before reading, but the body is then read whole (capped by serving.max_body_bytes) and parsed; nothing streams through. |
| 1 | partial | PRD-25 | predict | Active revision rule - an active deployment first, then the Production version, then the newest | No promoted_at column, so "newest promotion wins" is "newest version"; the tie-break is not covered by a test against real data (the lookup is faked in this test). |
| 1 | partial | REG-03 | registry | List model versions with customer stage labels | Rows carry name, version, stage and date only; metrics and the run link are reached through the lineage endpoint, so versions cannot be compared in one call. |
| 1 | partial | REG-11 | registry | model.json manifest (format, signature, labels, preprocessing, trainer version, dataset commit, metrics) | Input/output signature, class labels and preprocessing are written; trainer version, dataset commit and metrics summary are not verified present in model.json (not found in result.py). |
| 1 | partial | TRN-03 | training | Customer steps of a run (no raw job log, no report message) | Steps and a failure code only; the raw log is no longer served to customers (ops read Ray). No per-epoch metrics in the steps. |
| 1 | partial | TRN-06 | training | Presets are data, copied into the spec; project preset list for the training form | No GPU-minute estimate and no cost preview on the list (planning/22). |
| 1 | partial | TRN-29 | training | Licence policy - no AGPL trainer, licence scan and golden-metric tests in CI | No AGPL trainer is used (RT-DETRv2 via Transformers), but no licence scan runs in CI. |
| 1 | deferred | DAT-26 | datasets | Tombstoning dataset versions for erasure inside immutable history | Designed with the erasure flow. |
| 1 | deferred | DAT-27 | datasets | Labeling (Label Studio behind core-api) | Stretch slot in planning/20. |
| 1 | deferred | DAT-28 | datasets | Catalog admission for Hugging Face datasets (licence allowlist, mirror, admin flow) | Plus GET /v1/catalog/datasets and the catalog tables, which do not exist (GET /v1/catalog/models lists the presets' base models). |
| 1 | deferred | DEP-19 | deployment | Blue/green rollout, traffic split, serve-torch runtime | Later feature in the design. |
| 1 | deferred | JOB-09 | jobs | RayJob per run on ephemeral clusters with Kueue queues and priority classes | Staging uses a shared cluster; the rayjob backend exists but Kueue does not. |
| 1 | deferred | PRD-18 | predict | Streaming mode (Accept text/event-stream) with delta, done and error frames | No adapter declares streaming, so the route answers 406 not_acceptable (tested); streaming needs an LLM-style adapter first (planning/24 Q4). |
| 1 | deferred | PRD-19 | predict | Publishable keys (origin allowlist, CORS preflight, lower limit) | Stretch slot (planning/20 sprints 9 to 10); no api_keys.kind, no origins table. |
| 1 | deferred | PRD-20 | predict | Batch endpoint POST /v1/predict/{id}:batch | Later, for tabular. |
| 1 | deferred | SRV-12 | serving | Predict gateway as a separate streaming service (ADR-017) | Predict stays in core-api by decision (ADR-017 amended 2026-11-12). Split it into its own service when sustained load passes about 200 RPS or gateway overhead passes 10 ms p99. |
| 1 | deferred | SRV-14 | serving | Dynamic batching in Serve and batch inference job | Designed (kind predict_batch, @serve.batch); needs the owner's go-ahead. |
| 1 | deferred | TRN-11 | training | Tabular classification and regression (Ludwig), segmentation, LLM fine-tune | No trainer for these task types; the task catalog marks them not trainable (planning/20 slip rules drop Ludwig first). |
| 1 | deferred | TRN-34 | training | Streaming shard loader, node NVMe cache with prewarm, fast-start metrics | Trainer reads the staged files directly; designed for later (planning/20 sprint 9 to 10). |
| 1 | deferred | TRN-35 | training | Hyperparameter search (Ray Tune) and batch training variants | Presets first; tune is a product decision. |

## Built but not (well) tested

**Built with no test at all:** none (the test suite refuses this)

**Tested only by suites CI does not run** (`.github/workflows/tests.yml` runs core-api, ray-jobs, ray-training, ray-serving and the Postgres isolation matrix):

none

**Built, but the tests cited are whole files, not named behaviours** (look here for thin coverage):

| Id | Feature | Tests |
|---|---|---|
| SRV-15 | Inference latency and call metrics | core-api/tests/core/test_metrics.py |

## Could not be verified from the repo

| Id | Feature | Why |
|---|---|---|
| REG-11 | model.json manifest (format, signature, labels, preprocessing, trainer version, dataset commit, metrics) | Unverifiable here without running a trainer. |
| JOB-07 | Dedicated worker process (python -m app.worker) and an API that runs no pollers | The old audit rated this partial; the worker has tests now. Whether the k8s repo runs it as its own Deployment was not checked. |
| JOB-12 | Run-time integrity check of the package a job loads | The hash is checked when a package is registered; the job itself does not re-verify what it unpacks, and one staging key can still overwrite a package object (unverified here). |

## All features

### training

| Id | Status | Feature | Design source | Tests |
|---|---|---|---|---|
| TRN-01 | built | Start a run on a pinned dataset version (202, Idempotency-Key, 429 over the plan's concurrent runs) | planning/05 Conventions; planning/06 Job spec v1; docs/trainer-plan.md | 3 |
| TRN-02 | built | List and read runs (status, report, metrics, progress) | planning/05 Conventions | 2 |
| TRN-03 | partial | Customer steps of a run (no raw job log, no report message) | planning/05 Conventions; planning/11 Customer-visible job logs | 3 |
| TRN-04 | built | Cancel a run (stop the job, close the MLflow run, settle once) | planning/07 Cancellation and failure handling | 2 |
| TRN-05 | built | Job spec v1 `train` payload, validated against a committed JSON Schema | planning/06 Job spec v1; docs/trainer-plan.md Job spec | 3 |
| TRN-06 | partial | Presets are data, copied into the spec; project preset list for the training form | planning/06 Task types and runtimes; planning/22 Training form | 2 |
| TRN-07 | built | Admin CRUD of presets (changes never touch past runs) | planning/06 Task types and runtimes | 2 |
| TRN-08 | built | Image classification trainer (HF ConvNeXt) to ONNX with top-1, top-5 and confusion matrix | planning/06 Task types and runtimes | 2 |
| TRN-09 | built | Object detection trainer (RT-DETRv2) to ONNX with mAP50 and mAP50-95 | planning/06 Task types; planning/13 ADR-016 | 2 |
| TRN-10 | built | Text classification trainer (DistilBERT/BERT/RoBERTa), tokenizer served beside the model | planning/06 Task types (not listed there; added by docs/trainer-plan.md) | 2 |
| TRN-11 | deferred | Tabular classification and regression (Ludwig), segmentation, LLM fine-tune | planning/06 Task types and runtimes; planning/12 Thin demo | 0 |
| TRN-12 | built | Augmentation, LR schedule with warmup and early stopping pinned per preset | planning/17 Task types and trainers | 3 |
| TRN-13 | partial | Run state machine with compare-and-set transitions | planning/03 State machines; planning/13 ADR-008 | 2 |
| TRN-14 | partial | Trainer events API (idempotent by seq, only a running run takes events) | planning/06 Job runtime contract (Events) | 3 |
| TRN-15 | partial | Live progress (latest epoch and history) on the run | planning/06 Job runtime contract; planning/22 Metrics viewer | 1 |
| TRN-16 | built | Results from the job's report.json, never from logs | planning/06 Job runtime contract (Results) | 3 |
| TRN-17 | built | Metrics logged to MLflow by core-api; trainer holds no MLflow or lakeFS credential | planning/16 What a trainer receives; planning/13 ADR-018 | 2 |
| TRN-18 | built | Reconciler settles runs from Ray state plus report; billed once under replicas | planning/09 Reconciler design | 3 |
| TRN-19 | built | Concurrent-run quota per plan (429 too_many_training_runs) | planning/14 Quota enforcement | 2 |
| TRN-20 | deferred | Free-tier GPU-minute admission, 402 quota_exhausted, spend cap, minutes estimate | planning/14 Quota enforcement; planning/22 Training form | 0 |
| TRN-21 | partial | GPU-seconds metering of runs (usage event per finished run, once) | planning/14 Billable meters; planning/15 | 3 |
| TRN-22 | built | Fault tolerance - checkpoint per epoch, resume after worker loss, runtime limit | planning/16 Fast-start; planning/07 Scheduling | 3 |
| TRN-23 | built | Data-parallel training across workers (Ray Train), only rank 0 uploads | docs/ray-parallelism.md Built | 2 |
| TRN-24 | built | GPU memory sharing - declared vram_gb, per-process memory fraction, admission by sum of fractions | planning/17 GPU sharing | 2 |
| TRN-25 | built | Hash-based splits when none is given, a held-out test split | planning/06 Quality gates | 2 |
| TRN-26 | built | Deploy gate with acknowledge_risk, model card, near-duplicate warning | planning/06 Quality gates | 3 |
| TRN-27 | built | Base model pinned by commit, copied into the spec | planning/17 Admission policy; planning/06 Job spec rules | 4 |
| TRN-28 | deferred | Base model served from the platform mirror, never the Hub at run time; licence recorded | planning/17 Catalog; planning/06 Job spec rules | 3 |
| TRN-29 | partial | Licence policy - no AGPL trainer, licence scan and golden-metric tests in CI | planning/13 ADR-016; planning/17 Licence policy | 0 |
| TRN-30 | built | Lineage view - dataset version, run, model version, active deployment | planning/22 Lineage and pipeline view | 2 |
| TRN-31 | built | Task type decides what can train; untrainable and mismatched tasks refused before any job | planning/06 Task types and runtimes | 2 |
| TRN-32 | built | Hyperparameters validated against the preset's bounds | planning/06 Job spec rules (limits enforced by schema) | 2 |
| TRN-33 | partial | Training data to the trainer by presigned reads plus a staged manifest | planning/16 What a trainer receives (manifest endpoint with cursor and refresh) | 2 |
| TRN-34 | deferred | Streaming shard loader, node NVMe cache with prewarm, fast-start metrics | planning/16 Fast-start design | 0 |
| TRN-35 | deferred | Hyperparameter search (Ray Tune) and batch training variants | planning/06 Quality gates; docs/ray-parallelism.md Designed, not built | 0 |
| TRN-36 | built | Audit rows for run create and cancel | planning/19 E1 | 1 |
| TRN-37 | built | Class names backfilled from model.json into old reports | core-api maintenance (not designed) | 2 |

### registry

| Id | Status | Feature | Design source | Tests |
|---|---|---|---|---|
| REG-01 | built | Register a succeeded run's model (MLflow version plus model_versions row) | planning/06 Model registry and promotion | 2 |
| REG-02 | built | A succeeded run registers its model automatically at settle time | planning/06 Lifecycle | 1 |
| REG-03 | partial | List model versions with customer stage labels | planning/22 Models screen | 2 |
| REG-04 | built | Promote to Production deploys the version and flips the stage only when the deployment is active; the old Production goes to Staging, an older version is a rollback | planning/27 | 5 |
| REG-05 | built | Archived stage for retired versions | planning/06 Model registry and promotion (candidate, production, archived) | 1 |
| REG-06 | built | Promotion updates MLflow and the index in one operation | planning/06 Model registry and promotion | 2 |
| REG-07 | built | Stable model identity (models table) and revision, used by X-Model-Id / X-Model-Revision | planning/24 Q1; planning/05 Predict API | 2 |
| REG-08 | built | Tenant and project namespaced MLflow names, RLS-isolated model_versions index | planning/03 Who owns which data; planning/16 Adapter contract | 2 |
| REG-09 | deferred | Publish metadata and artifact flags to the public gallery | planning/05 (publish flag); planning/04 gallery | 4 |
| REG-10 | built | Stage ranks the default revision of a model (deploying needs no stage) | planning/06 Model registry and promotion | 1 |
| REG-11 | partial | model.json manifest (format, signature, labels, preprocessing, trainer version, dataset commit, metrics) | planning/06 Job runtime contract (Model manifest) | 1 |
| REG-12 | built | Stage enum mirrored by a CHECK with a drift test | core-backend CLAUDE.md rules | 1 |
| REG-13 | built | Audit rows for register, promote and publish | planning/19 E1 | 1 |
| REG-14 | partial | Download by signed URL and auto-generated model card | planning/18 Export; planning/06 Quality gates | 1 |
| REG-15 | built | Operator view of every model and its versions | planning/19 back office | 4 |
| REG-16 | built | Registering a run twice is idempotent (one version row per run) | planning/03 Idempotency and events | 2 |

### deployment

| Id | Status | Feature | Design source | Tests |
|---|---|---|---|---|
| DEP-01 | built | Create a deployment from a succeeded run (202, Idempotency-Key, predict key shown once) | planning/06 Deployment model; planning/05 Conventions | 3 |
| DEP-02 | built | List and read deployments (deleted rows hidden, internals hidden) | planning/05 Resource model | 2 |
| DEP-03 | built | Stop a deployment (predict answers 409, key kept, audited, idempotent) | planning/06 Deployment model; planning/18 Acceptable use enforcement | 2 |
| DEP-04 | built | Start a stopped deployment again (same row and key, takes a quota slot) | planning/06 Deployment model | 2 |
| DEP-05 | built | Delete a deployment (soft, key cleared, audited, frees the run) | planning/06 Deployment model | 2 |
| DEP-06 | partial | Deployment states and their legal moves, declared once and written only through job_lifecycle.advance | planning/03 Deployment state machine; planning/09 Reconciler design | 2 |
| DEP-07 | built | One live deployment per model version and per run (unique index, 409 deployment_exists) | planning/24 One active deployment per model version | 3 |
| DEP-08 | built | Plan limit on active deployments (429 before any deploy) | planning/14 Plans | 2 |
| DEP-09 | built | ETag and If-Match on deployments and settings | planning/05 Conventions (Concurrency) | 1 |
| DEP-10 | built | Audit rows for deployment actions | planning/19 E1; planning/18 | 2 |
| DEP-11 | built | Try it from the console is a real stored prediction (source=console); the try-it log lists them | planning/27 | 3 |
| DEP-20 | built | Deploy is keyed on a model version (model_version_id); one deployment serves exactly one version | planning/27 | 2 |
| DEP-12 | built | Deploy models of every servable task (classification, detection, text) | planning/06 Task types and runtimes | 2 |
| DEP-13 | partial | Redeploy a newer model onto the same endpoint (deploy_strategy main) and roll back by promoting | planning/06 Deployment model (Rollout, Rollback) | 2 |
| DEP-14 | built | Deployment input/output schema endpoint and generated curl/JS/Python snippets | planning/05 Predict API (Schema, Snippets) | 2 |
| DEP-15 | built | Active at once (the shared predictor apps load the model on the first request) | planning/03 Deployment state machine | 1 |
| DEP-16 | missing | Legacy per-deployment Serve app started by a Ray job (deprecated serving mode) | planning/12 Migration mechanics | 0 |
| DEP-17 | built | CPU or GPU compute target per project (project setting compute_target) | planning/07 Single-node reality | 1 |
| DEP-18 | deferred | Replica scale min/max per deployment limited by plan; scale-to-zero on the free tier | planning/06 Deployment model (Scale) | 0 |
| DEP-19 | deferred | Blue/green rollout, traffic split, serve-torch runtime | planning/06 Rollout; planning/17 Serving runtimes | 0 |

### serving

| Id | Status | Feature | Design source | Tests |
|---|---|---|---|---|
| SRV-01 | built | Two shared predictor apps (CPU, GPU) declared through the Serve REST API and kept declared | planning/06 Deployment model; planning/13 ADR-011; docs/ray-parallelism.md | 2 |
| SRV-02 | built | Multiplexed model loading by id (ONNX Runtime), recent models stay loaded | planning/17 Serving runtimes; docs/ray-parallelism.md | 2 |
| SRV-03 | built | Task adapters (classification softmax, detection boxes, text tokenize) from the manifest | planning/17 Serving runtimes | 2 |
| SRV-04 | built | Task types as data - trainer, serving functions, enabled flag, admin-editable | planning/06 Task types and runtimes | 3 |
| SRV-05 | built | Per-deployment in-flight cap and plan body cap | planning/07 Limits on the byte stream | 2 |
| SRV-06 | partial | Cold-start handling (503 with Retry-After, or a bounded wait) | planning/07 Serving design | 1 |
| SRV-07 | built | predict_calls log of every shared call with a retention purge | planning/15 U2 (per-prediction analytics, open) | 2 |
| SRV-08 | partial | Serving isolation - ONNX only, model URL allowlist, no redirects, pixel limit, pool pinning | planning/10 T8; planning/07 Single-node reality | 3 |
| SRV-09 | built | Autoscaling bounds and warm-model downscale delay from config | docs/ray-parallelism.md Serving scale | 1 |
| SRV-10 | built | Model URLs looked up once per run and cached; model fetched through the MLflow artifact API | docs/runtime-contract.md Models, experiments and MLflow | 2 |
| SRV-11 | built | Serve error codes mapped to HTTP status (422, 502, 503, 504), Serve's own text never returned | planning/24 Request flow, step 6 | 2 |
| SRV-12 | deferred | Predict gateway as a separate streaming service (ADR-017) | planning/07 Predict gateway; planning/20 sprint 5 | 0 |
| SRV-13 | built | Smoke check of the serving package before it can be promoted | planning/06 Job catalog (packages) | 2 |
| SRV-14 | deferred | Dynamic batching in Serve and batch inference job | docs/ray-parallelism.md Designed, not built | 0 |
| SRV-15 | built | Inference latency and call metrics | planning/11 Observability | 1 |

### predict

| Id | Status | Feature | Design source | Tests |
|---|---|---|---|---|
| PRD-01 | built | POST /v1/predict sync - model by headers, {input, metadata} body, envelope, input forwarded unchanged | planning/05 Predict API; planning/24 Request flow | 2 |
| PRD-02 | built | Error mapping (401, 403, 404, 409, 413, 422, 429, 502, 503, 504) as problem+json | planning/24 Request flow step 6 | 3 |
| PRD-03 | built | Project API keys - create (secret shown once, sha256 at rest), list, revoke, audited | planning/24 API keys; planning/04 Machine access | 3 |
| PRD-04 | built | Key scopes (enum plus CHECK) and optional per-model allowlist | planning/24 API keys | 3 |
| PRD-05 | built | require_key - one identical 401 for every failure cause, X-Project-Id must match the key | planning/24 require_key; planning/04 Authorization | 2 |
| PRD-06 | built | Suspended tenant answers 503 tenant_suspended before Serve is called; archived projects answer like an unknown key | planning/24 require_key; planning/14 Billing states | 3 |
| PRD-07 | partial | Rate limits - plan, per key (api_keys.rate_limit_per_minute) and per deployment, 429 with Retry-After | planning/24 Rate limits; planning/05 Conventions | 3 |
| PRD-08 | built | Idempotency-Key on sync calls - stored result replayed, 422 on a different body, race-safe | planning/24 Idempotency-Key | 3 |
| PRD-09 | built | Background mode - Prefer respond-async returns 202 with Location and a queued envelope | planning/24 Modes | 2 |
| PRD-10 | built | GET a prediction (only by the key that made it; expired payloads keep id and status) | planning/24 Background endpoints | 3 |
| PRD-11 | built | Cancel a prediction (queued ends at once, running flagged, terminal is an idempotent 200) | planning/24 Background endpoints | 3 |
| PRD-12 | built | Prediction status SSE with Last-Event-ID replay and heartbeat | planning/24 Background endpoints | 3 |
| PRD-13 | built | Prediction worker - claim with lease (SKIP LOCKED), attempt fencing, retry with backoff, bounded concurrency | planning/24 Poller, leader election and fencing | 4 |
| PRD-14 | built | Payloads over predict.inline_max_bytes stored in the staging bucket under predictions/<id>/<attempt>/ | planning/24 predictions table | 3 |
| PRD-15 | built | Retention purge of stored predictions (payloads, objects, events), keeping id and status | planning/24 Retention | 2 |
| PRD-16 | partial | Usage - one event per 2xx prediction id, none for failures, replays or retries | planning/24 Usage and billing; planning/14 | 3 |
| PRD-17 | partial | Legacy POST /v1/predict/{deployment_id} with X-Predict-Key, marked Deprecation and Sunset, counted per deployment | planning/24 Migrating the old endpoint (phase 1) | 2 |
| PRD-18 | deferred | Streaming mode (Accept text/event-stream) with delta, done and error frames | planning/24 Modes; planning/05 Streaming | 1 |
| PRD-19 | deferred | Publishable keys (origin allowlist, CORS preflight, lower limit) | planning/24 Publishable keys; planning/05 Auth | 0 |
| PRD-20 | deferred | Batch endpoint POST /v1/predict/{id}:batch | planning/05 Predict API (Batch) | 0 |
| PRD-21 | partial | Input forms - multipart, base64 JSON, image URL with SSRF guard, decompression-bomb guard | planning/05 Predict API (Input) | 1 |
| PRD-22 | built | Metadata (at most 4 KB) echoed in the envelope, never forwarded to Serve | planning/05 Predict API; planning/24 Parse | 2 |
| PRD-23 | partial | Key rotation or recovery, last-used time, first-use audit, publishable-key nudges | planning/04 Machine access; planning/22 Keys; planning/10 T13 | 1 |
| PRD-24 | partial | Authenticate, rate-limit and size-check before reading the body; stream with limits on the byte stream | planning/07 Predict gateway rules; planning/24 Parse | 1 |
| PRD-25 | partial | Active revision rule - an active deployment first, then the Production version, then the newest | planning/24 One active deployment per model version | 1 |
| PRD-26 | built | Predictions table is tenant-isolated and a key sees only its own | planning/24 predictions table; planning/24 Security notes | 2 |

### jobs

| Id | Status | Feature | Design source | Tests |
|---|---|---|---|---|
| JOB-01 | built | Versioned job spec v1 - shared envelope, payload per kind, unknown version or kind refused | planning/06 Job spec v1; docs/job-spec.md | 3 |
| JOB-02 | built | JSON Schema generated from the pydantic models and kept equal by a test | planning/06 Job spec v1 | 1 |
| JOB-03 | partial | Envelope fields attempt and reporting (events_url, token_env) | planning/06 Job spec v1 | 1 |
| JOB-04 | deferred | One jobs table with kind, spec, state and attempt | planning/03 Entities; planning/13 ADR-007 | 0 |
| JOB-05 | built | Shared follower engine - tenant loop, lease claim (SKIP LOCKED), compare-and-set advance, backoff, isolation of a failing row | planning/09 Reconciler design; docs/runtime-contract.md Worker mode | 3 |
| JOB-06 | built | Singleton sweeps elected through Postgres advisory locks; claiming pollers run on every replica | planning/09 State management rules; planning/13 ADR-006 | 2 |
| JOB-07 | built | Dedicated worker process (python -m app.worker) and an API that runs no pollers | planning/01 Responsibilities; planning/08 I5 | 2 |
| JOB-08 | built | Compute seam with two backends - Ray dashboard job API and KubeRay RayJob CR | planning/07 As built; planning/13 ADR-010 | 2 |
| JOB-09 | deferred | RayJob per run on ephemeral clusters with Kueue queues and priority classes | planning/07 Target topology; planning/13 ADR-010 | 0 |
| JOB-10 | partial | Job catalog - a JobType names its entrypoint module | planning/06 Job catalog | 1 |
| JOB-11 | built | Job packages - content-hash zips, candidate to stable to retired, smoke gate, CI publish | planning/06 Where code lives (zip only); docs/runtime-contract.md | 4 |
| JOB-12 | partial | Run-time integrity check of the package a job loads | planning/10 T15 | 1 |
| JOB-13 | built | Results from report.json written by a shared runner that cannot skip it | planning/06 Job runtime contract (Entry, Results) | 3 |
| JOB-14 | partial | Lost-job detection - suspect on silence, confirm with the compute API, then fail and free the slot | planning/06 Job runtime contract (Heartbeats); planning/09 Failure modes | 2 |
| JOB-15 | missing | Attempt fencing - attempt in token, prefixes and events; stale attempts rejected | planning/06 Job spec rules; planning/19 C6 | 0 |
| JOB-16 | built | Transactional outbox, dispatcher and webhooks | planning/03 Idempotency and events; planning/13 ADR-005; planning/05 Events and webhooks | 4 |
| JOB-17 | missing | Reconciler wake-up through LISTEN/NOTIFY | planning/09 Reconciler design | 0 |
| JOB-18 | built | Bounded per-tenant concurrency and failure isolation in sweeps | planning/09 Reconciler design | 2 |
| JOB-19 | built | Resources declared in the spec, never inferred; runtime limit enforced | planning/06 Job spec rules; planning/07 Timeouts | 2 |
| JOB-20 | deferred | Per-job prefix-scoped storage credential (Credential Access Boundary or per-import STS) | planning/16 What a trainer receives; planning/10 T19 | 0 |
| JOB-21 | missing | credential_issuances ledger of minted credentials | planning/16 Control mapping | 0 |
| JOB-22 | partial | Suspending a tenant stops what is already running (jobs, deployments, sweeps) | planning/14 Billing states; planning/19 A-04 | 1 |
| JOB-23 | partial | Per-plan limits stored as data and edited by an operator (imports, runs, archives, images, tables, predict, deployments) | planning/14 Plans | 3 |
| JOB-24 | built | Ingest jobs run resource-limited with the staging credential and no lakeFS credential | planning/06 Datasets; planning/13 ADR-018 | 2 |
| JOB-25 | partial | Contract tests as a hard merge gate (job spec, report schema, trainer contract, DB isolation) | planning/20 Gates for a single-reviewer team | 2 |
| JOB-26 | partial | Journey scripts (import, train and deploy, predict) run against the local stack | planning/05 Machine-friendly by design | 0 |
| JOB-27 | built | Customer-facing events for jobs (job.queued .. job.succeeded) and notifications | planning/05 Events and webhooks; planning/05 Notification channels | 3 |

### datasets

| Id | Status | Feature | Design source | Tests |
|---|---|---|---|---|
| DAT-01 | built | Create, list (cursor) and read datasets | planning/05 Resource model; docs/datasets-plan.md | 3 |
| DAT-02 | deferred | Soft delete, restore, and 7-day purge of the lakeFS repo, namespace and rows | planning/03 Retention; docs/datasets-plan.md | 4 |
| DAT-03 | built | Create an import - presigned PUT to a fixed staging key, upload never passes through the API | planning/06 Datasets | 2 |
| DAT-04 | built | Import lifecycle - 10 states, compare-and-set transitions, Idempotency-Key, per-tenant active cap | planning/05 Resource model; planning/03 State machines; docs/datasets-architecture.md | 4 |
| DAT-05 | built | List and read imports | docs/datasets-architecture.md | 1 |
| DAT-06 | built | Start an import (queue it and submit the ingest job; a failed submit stays startable) | docs/datasets-architecture.md Import flow | 3 |
| DAT-07 | built | Cancel an import (stops the job, purges staging) | docs/datasets-architecture.md | 2 |
| DAT-08 | built | Review before commit - staged preview and report, discard, expiry (24 h upload, 7 d review) | docs/datasets-plan.md decision 10 (not in planning) | 4 |
| DAT-09 | built | Commit - staged output verified, copied inside the object store, one idempotent lakeFS commit | planning/06 Datasets; planning/16 | 3 |
| DAT-10 | built | Merge an import onto a base version (strict merge, 409 stale_base, duplicates skipped, no overwrite mode) | docs/datasets-plan.md decision 5 (not in planning) | 3 |
| DAT-11 | built | Immutable dataset versions, listed | planning/03 Entities; planning/06 Datasets | 1 |
| DAT-12 | built | Version preview with per-image presigned reads | planning/22 section 8 | 3 |
| DAT-13 | deferred | Publish a dataset version to the public gallery (stats only) and unpublish | planning/05 (publish flag); planning/04 | 4 |
| DAT-14 | built | Ingest image classification from a zip | planning/06 Task types | 2 |
| DAT-15 | built | Ingest object detection from a COCO zip | planning/06 Task types | 2 |
| DAT-16 | built | Ingest tabular and text datasets from CSV (target column, split tag, schema inference) | planning/06 Task types; planning/12 thin demo | 2 |
| DAT-17 | built | Hostile-archive defences - traversal, symlinks, depth, file count, size, ratio, byte budget, header decode | planning/06 Datasets; planning/10 T7 | 4 |
| DAT-18 | built | Hugging Face dataset import (card and schema check, pinned commit, data files only, size cap) | planning/17 Hugging Face datasets; planning/12 P3 | 3 |
| DAT-19 | partial | Canonical formats - WebDataset shards (vision) and Parquet (tabular) | planning/06 Datasets; planning/16 Fast-start | 1 |
| DAT-20 | partial | Dataset statistics and health report from ingest (class balance, counts, corrupt files, duplicates) | planning/06 Quality gates; planning/16 Dataset statistics | 2 |
| DAT-21 | built | Perceptual-hash warning for near-duplicates across splits | planning/06 Quality gates | 2 |
| DAT-22 | built | Per-plan dataset limits (upload size, extracted size, files, image size, rows, HF repo size, concurrent imports) | planning/14 Plans | 3 |
| DAT-23 | built | Audit rows for dataset actions | planning/19 E1 | 3 |
| DAT-24 | built | Import usage metering (count and bytes) | planning/15 Usage pipeline | 1 |
| DAT-25 | deferred | datasets.data_categories declaration (personal data, faces and biometrics) | planning/18 Backend responsibilities | 1 |
| DAT-26 | deferred | Tombstoning dataset versions for erasure inside immutable history | planning/03 Tables specified elsewhere; planning/18 | 0 |
| DAT-27 | deferred | Labeling (Label Studio behind core-api) | planning/16 Label Studio; planning/22 | 0 |
| DAT-28 | deferred | Catalog admission for Hugging Face datasets (licence allowlist, mirror, admin flow) | planning/17 Catalog | 0 |
| DAT-29 | built | Adapter contract - only typed ids, names derived, ownership resolved under RLS first, fails closed | planning/16 Adapter contract | 3 |
| DAT-30 | built | Operator list of a tenant's datasets | planning/19 back office | 2 |
| DAT-31 | partial | Cleanup of per-run training prefixes in the staging bucket | planning/03 Retention; planning/18 Erasure | 2 |
