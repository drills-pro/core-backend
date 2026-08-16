# Usage metering (customer billing)

## Relationship to 08-observability-lgtm.md
OpenCost (08) estimates internal cloud spend for margin visibility — approximate, reconciled monthly, not the billing source of truth. This doc defines the metering system that actually determines what a customer is charged. The two must be reconciled periodically (see FR5) but are not the same system.

## Billed dimensions
| Dimension | Unit | Source | Cadence |
|---|---|---|---|
| Training compute | GPU-seconds, by GPU type | Ray job start/end timestamps captured by the orchestration service at job completion | Event per job |
| Dataset storage | GB-days | Object storage bytes for tenant's S3/MinIO prefix (or LakeFS repo size once introduced) | Daily snapshot |
| Model storage | GB-days | Model artifact size per stored version in MLflow's artifact store | Daily snapshot |
| Inference compute | Compute units — GPU-seconds consumed by the deployment, or a normalized unit if you want cross-model-size fairness (formula TBD, see open questions) | Ray Serve replica metrics via Prometheus, aggregated per deployment | Hourly rollup |

## Data model
```
usage_events (
  id,
  tenant_id,
  project_id,
  resource_type,   -- training | storage_dataset | storage_model | inference
  quantity,
  unit,
  occurred_at,
  source_ref,       -- job_id, snapshot_id, or rollup_id — for idempotency + audit trail
  pricing_version    -- see FR3, so historic invoices don't shift under you
)
```
Append-only. Never update or delete a row — corrections are new offsetting rows, so the ledger stays auditable.

## Requirements
- FR1: Every completed training job emits exactly one `usage_events` row keyed by `job_id` (idempotent — a retried completion webhook must not double-count).
- FR2: Daily batch job snapshots dataset and model storage per tenant into `usage_events` as GB-days, not a single GB figure — otherwise a customer who uploads 500GB and deletes it same-day looks identical to one who holds it all month.
- FR3: Define and version the inference "compute unit" formula explicitly (`pricing_version` column) before implementation — if the formula changes later, past invoices must still be reproducible from the version that was active when the usage occurred.
- FR4: Tenant can query their own raw `usage_events` through the API (04-api.md) at any point mid-billing-period — don't make usage visibility a month-end-only report.
- FR5: Monthly reconciliation job diffs `usage_events`-derived internal cost against OpenCost's cloud-cost estimate; large drift is a signal to investigate (mispriced GPU type, missed events), but `usage_events` is what gets invoiced regardless of what OpenCost says.
- NFR1: Metering writes must be durable and replayable from raw events — never aggregate only in memory and discard the source events.
- NFR2: Idempotency key on every write path (job completion callback, storage snapshot job, inference rollup job) — retries are a certainty in a distributed system, double-billing from a retry is not acceptable.

## Open questions
- Pricing model itself (flat per-GPU-hour, tiered plans with included quota + overage, per-seat + usage hybrid) is not decided here — this doc is the metering plumbing, not the price list. Needs a decision before FR3's formula can be finalized.
- Inference compute-unit normalization across model sizes/hardware — needs a worked example (e.g., a YOLO-nano detect deployment vs a large segmentation model) before implementation; raw GPU-seconds may already be the simplest defensible unit rather than inventing a synthetic one.
