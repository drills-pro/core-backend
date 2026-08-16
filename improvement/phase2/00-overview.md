# Phase 2 — production no-code MLOps platform, specification index

## Purpose
Phase 1 (this repo's current state) proved the architecture end-to-end: real
multi-tenant auth, real Ray/Ludwig/Ultralytics training, real LakeFS
versioning, real MLflow registry, real Ray Serve deployment — but as a
single-project, single-user-role demo. Phase 2 turns that proof into a
product: orgs/teams, project-scoped objectives, dataset governance,
labeling, OAuth account linking, public sharing, and a UI that doesn't
assume the person reading it already knows what a confusion matrix is.

## Documents
| # | Doc | Scope |
|---|---|---|
| 01 | [architecture-principles](01-architecture-principles.md) | The build-vs-reuse rule this whole phase is judged against |
| 02 | [org-team-project-model](02-org-team-project-model.md) | Organization → Team → Project hierarchy, project = one objective |
| 03 | [dataset-governance](03-dataset-governance.md) | Task-typed datasets, min-sample gate, train/test pairing, format import (Ludwig table, HF, YOLO/COCO) |
| 04 | [labeling-preprocessing](04-labeling-preprocessing.md) | Raw-upload → labeled pipeline, low-code dataset preprocessing on Ray |
| 05 | [reproducibility-lineage](05-reproducibility-lineage.md) | Dataset commit → training run → model version → deployment, as one traceable chain |
| 06 | [project-settings-deployment](06-project-settings-deployment.md) | Compute target, base model choice, main-vs-latest deploy strategy, deployment status fix |
| 07 | [identity-oauth-linking](07-identity-oauth-linking.md) | Email-anchored account identity, OAuth (Google/GitHub) linked not duplicated, profile settings |
| 08 | [collaboration-visibility](08-collaboration-visibility.md) | Project invites/roles, public/private projects, publishing datasets/models |
| 09 | [usage-billing-two-tier](09-usage-billing-two-tier.md) | Project-level and org-level usage rollups |
| 10 | [ui-revamp](10-ui-revamp.md) | Information architecture for the multipage product, page map, design direction |
| 11 | [migration-phasing](11-migration-phasing.md) | Order of work, what's additive vs. breaking against the current schema |

## Non-negotiable constraints (carry into every doc)
- **No new infrastructure component unless nothing already running can do the job.** This stack already runs Zitadel, Postgres, LakeFS, Ray (+Ray Data, Ray Serve), MLflow, Ludwig, Ultralytics, Label Studio, Vault, LGTM. Phase 2 is disciplined about *using* that surface area fully before reaching for anything new — see 01.
- Every customer-facing concept (dataset version, model version, training run, deployment) stays a durable, queryable Postgres row with RLS tenant isolation — never a log line or a Ray-internal-only state, matching the pattern already established in `core-api/app/`.
- Every background transition (job status, deploy status) gets a poller/reconciler from day one — Phase 1 shipped three separate instances of "status stuck because nothing re-polled Ray" (training runs, dataset imports, deployments) before this was made a standing rule. New async state machines copy the existing `reconcile_inflight_*` pattern in `core-api/app/training.py` / `datasets.py` / `deployments.py`, not a bespoke one.
- Backward compatible with the running schema where reasonable; where not (see 02's tenant→org split), documented as an explicit migration in 11, not a silent break.

## Out of scope for this phase (explicitly deferred)
Multi-region, enterprise SSO/SCIM, a visual pipeline/DAG builder beyond the step-list described in 04, real-money billing integration (usage rollups land in 09; a payment processor does not), fine-grained per-object ACLs beyond project-role + public/private.

## Status
Draft — v0.1, written against the codebase state at the end of Phase 1 (Zitadel auth, Ray training with numpy/Ultralytics/Ludwig backends, LakeFS dataset versioning, MLflow registry, Ray Serve deployment, single-tenant-per-org model). Not yet reviewed against a design partner.
