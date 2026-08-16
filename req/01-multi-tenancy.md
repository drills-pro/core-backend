# Multi-tenancy

## Tenant model
- Tenant = customer org (Zitadel "organization"). One org can have many projects, many users, many datasets, many models.
- Every persisted record (dataset, label, training job, model version, deployment) carries `tenant_id`. No cross-tenant joins in application queries — enforce at the ORM/query-builder layer, not just convention.

## Isolation requirements
| Layer | Isolation mechanism | Level |
|---|---|---|
| Auth | Zitadel organization | Hard (separate user pools, separate SSO config per org) |
| App DB (Postgres) | Row-level `tenant_id` + Postgres RLS policies | Logical |
| Object storage | Per-tenant prefix (`s3://bucket/tenants/{tenant_id}/...`) | Logical |
| Training compute | Ray job submitted with `tenant_id` label; no shared process state between tenant jobs | Logical (process-level) |
| Inference compute | Ray Serve deployment tagged `tenant_id`; model weights loaded per-tenant, never shared across tenants even if architecture is identical | Logical |
| Cost | OpenCost allocation by `tenant_id` label on every K8s resource | Reporting only, not enforcement |

## Explicitly not required for v1
- Physical/network isolation per tenant (separate VPC, separate cluster). Revisit if a customer's compliance requirement demands it — treat as a paid enterprise tier, not default.
- Per-tenant encryption keys (BYOK). Flag as a future enterprise-tier item.

## Requirements
- FR1: A user authenticated into org A must receive a 403, not a 404 or empty result, when addressing org B's resource IDs directly (avoid ID enumeration ambiguity, but don't leak existence either — decide and document one behavior).
- FR2: Deleting a tenant must cascade-delete or cascade-anonymize all datasets, labels, models, and deployments within a bounded SLA (define: 30 days soft-delete, then hard delete).
- FR3: A tenant must be able to see its own resource usage/cost broken out by project.
- NFR1: Noisy-neighbor protection — one tenant's training job must not starve another tenant's inference serving on the same node pool. Enforced via K8s resource requests/limits + separate node pools for training vs serving (see 07).

## Open questions
- Do we support a tenant having sub-tenants (e.g., an agency managing multiple end-client orgs)? Not decided — affects whether Zitadel org hierarchy or a custom `parent_org_id` is needed.
