# Usage — project level and org/team level

## Current state
`usage_events` (Phase 1) records training GPU-seconds per
`tenant_id, project_id`; `GET /v1/usage` (org-wide) and
`GET /v1/projects/{id}/usage` (project) both already exist as `SUM()`
queries — the two-tier read path is *already built*, just currently
tenant/project only, no team layer, and (noted in Phase 1 close-out)
inference calls aren't metered at all yet despite `req/09`'s own spec
calling for it.

## What Phase 2 actually adds
FR1: `team_id` on `usage_events` (backfilled via `project.team_id`, per
02) so team-level rollup (`GROUP BY team_id`) is a real third tier
without a new table — org (existing) → team (new) → project (existing).
FR2: Meter inference: `deployments.py`'s `/predict` handler gains a
`record_usage(resource_type="inference", quantity=1, unit="requests",
source_ref=f"{deployment_id}:{request_id}")` call — closing the gap
flagged at the end of Phase 1. `source_ref` includes a request id (not
just the deployment id) so the existing `UNIQUE(resource_type,
source_ref)` idempotency constraint doesn't collapse distinct requests
into one row.
FR3: UI: a project's Usage tab (new) shows its own rollup; an
org/team-level Usage page (new, under the team settings area) shows the
sum across all member projects — same data, two aggregation levels,
matching the literal requirement ("usage have 2 level").

## Explicitly not in scope
A real payment/invoicing integration — 09's `PRICING_VERSION` versioning
scheme (already built) is what makes turning raw usage into a dollar
figure *possible* later without re-deriving history; actually wiring a
payment processor is a separate, later initiative this doc doesn't
claim to solve.
