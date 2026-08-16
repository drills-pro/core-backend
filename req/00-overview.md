# No-code ML/DL platform — specification index

## Purpose
Platform for non-technical customers to bring their own image datasets, label them, train/finetune detection/segmentation/classification models (later: time series), version and deploy models, without understanding train/test splits, hyperparameters, or infra.

## Documents
| # | Doc | Scope |
|---|---|---|
| 01 | [multi-tenancy](01-multi-tenancy.md) | Tenant isolation model across data, compute, cost |
| 02 | [auth-identity](02-auth-identity.md) | Zitadel vs Authentik decision, OIDC flows, RBAC |
| 03 | [project-user-management](03-project-user-management.md) | Orgs, projects, roles, invitations |
| 04 | [api](04-api.md) | Public/internal API surface, versioning |
| 05 | [ui-nocode-builder](05-ui-nocode-builder.md) | Drag-drop builder, labeling UX, model versioning UX |
| 06 | [ml-training-mlflow-ludwig](06-ml-training-mlflow-ludwig.md) | Training backends, registry, dataset versioning |
| 07 | [compute-ray-k8s](07-compute-ray-k8s.md) | K8s, KubeRay, Ray Train/Tune/Serve, autoscaling |
| 08 | [observability-lgtm](08-observability-lgtm.md) | Loki/Grafana/Tempo/Mimir(or Prometheus), internal margin tracking |
| 09 | [usage-metering](09-usage-metering.md) | Customer-billed usage: training compute, storage, inference — separate from internal cost |

## Non-negotiable constraints (carry into every doc)
- Open source first; commercially usable licenses only (Apache 2.0 / MIT / BSL-with-carveout reviewed case by case).
- Every customer-facing concept (dataset version, model version, training run) must map to a durable, queryable artifact — not just a log line.
- No component may block a solo "glue" team from operating it (no components requiring a dedicated ops specialist for v1).

## Out of scope for v1 (explicitly deferred, see 07 for phasing)
Argo Workflows, LakeFS, self-hosted identity SSO for enterprise customers, multi-region.

## Status
Draft — v0.1. Author: Patrick. Not yet reviewed against a design partner.
