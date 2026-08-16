# Architecture principles — build vs. reuse

## The rule
Before writing a new service, table, or dependency for a Phase 2 feature,
name the already-running component that does 80% of the job, and write
only the 20% glue. This document is the checklist every other Phase 2 doc
is judged against.

## What's already running, and what it's for
| Component | Already does | Phase 2 leans on it for |
|---|---|---|
| **Zitadel** | OIDC, org/user model, session API, embedded login | OAuth federation (Google/GitHub) as *linked IdPs on the same Zitadel user*, not a second auth system (07) |
| **Postgres + RLS** | Tenant-isolated projects/datasets/runs/deployments | Org→Team→Project hierarchy, invites, roles — more tables, same isolation pattern (02, 08) |
| **LakeFS** | Per-dataset branch + commit versioning (built in Phase 1) | Train/test dataset pairing, lineage source-of-truth (03, 05) |
| **Ray (jobs + Data + Serve)** | Training job submission, already-versioned data reads | Preprocessing pipelines (04) and deployment (06) — no separate pipeline engine |
| **MLflow** | Run tracking, model registry stages | The canonical model-version record — Postgres `model_versions` stays a pointer into it, not a duplicate (05) |
| **Ludwig** | Declarative YAML training config | Doubles as the low-code *preprocessing* spec too — see 04's recommendation, not a new DSL |
| **Label Studio** | Running in `docker-compose.yml`, never wired to anything | The raw→labeled pipeline (04) — a real labeling UI already exists, don't build one |
| **Vault** | Dynamic Postgres creds, static KV (LakeFS/MinIO/Zitadel secrets) | Unchanged; new secrets (OAuth client secrets) go in the same KV pattern |

## Decision log (why not X)
- **Why not a workflow engine (Argo/Airflow/Prefect) for preprocessing?** Ray Data already expresses a linear step pipeline (map → filter → resize → write) as plain Python callables submitted as a Ray job — the exact mechanism training already uses. A DAG engine is justified once steps branch/fan-out across independent datasets; Phase 2's preprocessing is per-dataset linear steps. Revisit if a customer need for branching pipelines appears.
- **Why not a message queue (Kafka/RabbitMQ/NATS)?** Asked and answered mid-Phase-1: the one producer (a 10s reconciler poll) and one consumer (Postgres) don't have a backpressure or fan-out problem a queue solves. Revisit only if a genuine multi-consumer fan-out need appears (e.g., webhooks to external systems on every status change) — see 06's deployment-events note for where that threshold might first be crossed.
- **Why not a separate "billing service"?** Usage is already Postgres rows (`usage_events`) aggregated by `SUM()`. Two-tier rollup (09) is one more `GROUP BY`, not a service.
- **Why not NextAuth/Auth0/Clerk for OAuth linking?** Zitadel already terminates OIDC and supports configuring external IdPs (Google, GitHub) as login options on the *same* Zitadel instance/org — federation is Zitadel's job description. A second auth layer in front of Zitadel would duplicate session/token handling core-api already has working.
- **Why keep Ludwig config as the "low-code" surface instead of a drag-drop pipeline builder?** Ludwig's YAML *is* a low-code spec — a UI form that emits Ludwig config keys is less code than a general visual DAG builder, and every generated config is directly runnable/inspectable/versionable as a file. See 04.

## What counts as "new infra" that still needs justifying
Adding a library inside an existing Ray-image job (e.g., `albumentations` for image augmentation, already Python, already runs inside the same job process) is not new infra — it's a dependency the same way `ultralytics`/`ludwig` already are. New infra means a new *service* in `docker-compose.yml`/`k8s/`. The bar is much higher for the latter.
