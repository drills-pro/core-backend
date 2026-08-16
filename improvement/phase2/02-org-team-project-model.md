# Organization → Team → Project model

## Current state (Phase 1)
`tenants` (1:1 with a Zitadel org) → `projects` → `datasets`/`training_runs`/
`deployments`. One tenant = one flat bucket of projects; no team layer, no
per-project role beyond "has a valid tenant token."

## Target hierarchy
```
Organization (= today's tenant, 1:1 Zitadel org)
  └── Team (new)              — usage rollup boundary, invite boundary
        └── Project (existing) — one objective, own dataset(s)/runs/deployments
```
FR1: An org has ≥1 team by default (a "Default" team created at org
provisioning, same place `tenants` rows are seeded today —
`scripts/zitadel-bootstrap.sh`'s tenant-seed step gains a team-seed step).
FR2: A project belongs to exactly one team. Moving a project between teams
is a Phase 3 concern (not blocked, just not built).
FR3: `usage_events` gains a `team_id` (backfilled from `project.team_id`)
so 09's two-tier rollup has a real join path without re-deriving it.

## Project = one objective
FR4: `projects` gains a `task_type` column, set at creation, immutable
after the first dataset is attached:
`image_classification | object_detection | tabular_classification | text_classification`.
FR5: `task_type` is the gate 03 (dataset governance) checks against on
every dataset attach/import — a dataset whose shape doesn't match the
project's task type is rejected with a specific error, not silently
accepted and failed at training time.
FR6: Existing Phase-1 projects (no `task_type`) are migrated to a
`legacy_untyped` value that bypasses the gate — see 11 for the exact
migration; this is the one place Phase 2 tolerates an escape hatch for
already-running demo data rather than forcing a backfill decision it
can't make on the customer's behalf.

## Roles
FR7: Reuse the role vocabulary Phase 1's `scripts/zitadel-bootstrap.sh`
already provisions in Zitadel (`org_owner`, `org_admin`, `project_editor`,
`project_viewer`) — Phase 2 adds *project-scoped* role assignment
(`project_members` table already exists per `app/project_members.py`;
extend it to carry the role, don't invent a parallel table).
FR8: `project_viewer` on a private project can see runs/metrics/models but
not download raw dataset files or the predict key — enforced at the API
layer (`app/tenancy.py`'s dependency chain), not just hidden in the UI.

## Non-goals
Nested teams, cross-org projects, per-resource ACLs finer than
project-role — all deferred; team is a flat grouping under one org, not a
second recursive hierarchy.
