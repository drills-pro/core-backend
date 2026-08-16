# API

## Shape
REST + JSON over HTTPS, `api.yourdomain.com/v1/...`. No GraphQL for v1 — no evidence yet that the client needs flexible querying over a fixed no-code UI.

## Surface (grouped by resource, not exhaustive)
| Group | Endpoints | Notes |
|---|---|---|
| Auth | delegated to Zitadel OIDC | app only validates tokens, doesn't issue them |
| Projects | CRUD + members | see 03 |
| Datasets | upload, list, get, delete, trigger labeling export | upload returns presigned S3/MinIO URL, client PUTs directly (don't proxy large files through app backend) |
| Labeling | proxy/webhook to Label Studio project, pull labeled export | |
| Training | create run (config + dataset commit), get status, list runs, cancel | submits to Argo/Ray via internal service, not directly from client |
| Models | list versions, promote version (staging→production), rollback | wraps MLflow Model Registry API |
| Deployments | create/update/delete, get status, get inference endpoint | wraps Ray Serve / BentoML deployment API |
| Inference (per-tenant) | `POST /v1/predict/{deployment_id}` | separate auth scope from management API — this is what the customer's own downstream systems call |
| Usage/cost | get project usage breakdown, get org usage breakdown | reads from `usage_events` (09-usage-metering.md) — the billing source of truth, not OpenCost |

## Requirements
- FR1: All mutating endpoints (POST/PUT/DELETE) require org-scoped RBAC check before touching data — enforced in a shared middleware, not per-handler.
- FR2: Idempotency keys required on training-run creation and deployment creation (client retries on network failure must not double-submit a GPU job).
- FR3: Long-running operations (training, deployment rollout) return a job resource immediately (202) with a status URL — never block an HTTP request on a multi-minute training job.
- FR4: Rate limiting on the inference endpoint per deployment, configurable per tenant plan tier.
- NFR1: API versioned in the URL path (`/v1/`); breaking changes require a new version, not in-place mutation of `/v1/`.
- NFR2: OpenAPI spec generated from code (FastAPI native), published for customers who want to call the management API directly (not just through the UI).

## Open questions
- Webhooks for training-complete / deployment-ready events — needed once customers want to integrate rather than watch the UI. Not v1, but design the job-status model so adding webhooks later doesn't require a schema change.
