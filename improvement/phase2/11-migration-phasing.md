# Migration and phasing

## Ordering principle
Land schema/backend changes a full step ahead of the UI that depends on
them (matches how Phase 1 itself was built — endpoint, curl-verified,
then wired to UI) and land additive-only migrations before the one
genuinely structural change (team layer), so the app stays deployable at
every commit.

## Suggested phase order
1. **02 (org/team/project)** — additive: new `teams` table, `team_id` on
   `projects` (nullable at first, backfilled to each org's auto-created
   "Default" team, then made `NOT NULL`). `task_type` on `projects`
   (nullable → `legacy_untyped` backfill → stays nullable-with-default
   forever for that escape hatch, per 02 FR6).
2. **03 (dataset governance)** — additive: YOLO/COCO importers are new
   endpoints; the task-type gate is a new `if` in existing endpoints,
   feature-flaggable per-project during rollout (skip the check for
   `legacy_untyped` projects, per 02).
3. **06 (project settings + deploy bug fix)** — the deployment
   reconciler fix ships independently and immediately (already done,
   see 06's context note); `project_settings` jsonb column is additive;
   `deploy_strategy: main` is new deploy-time logic, `latest` (default)
   is a no-behavior-change path.
4. **07 (identity/OAuth)** — additive: new `users` table (profile,
   username) keyed by Zitadel user id, populated lazily on first login
   post-launch (no backfill needed — a user without a `users` row just
   hasn't visited `/settings/profile` yet, defaults apply). Zitadel IdP
   registration is config, zero app-code risk.
5. **08 (collaboration/visibility)** — additive: `project_invites`,
   `visibility`/`publish_*` columns default to `private`/`false`, so
   existing projects are unaffected until an owner opts in.
6. **04 (labeling/preprocessing)** — additive, and the largest net-new
   surface area (Label Studio wiring, Ray Data preprocessing executor) —
   ordered last because it depends on 03's task-type gate existing to
   know what labeling UI a given dataset needs.
7. **09 (usage two-tier)**, **05 (lineage view)**, **10 (UI revamp)** —
   these three are read-side/UI work layered on top of everything above
   and can proceed in parallel with each other once 02/03/06/07/08's
   schema exists, since none of them need new write paths of their own.

## Rollback posture
Every migration in this phase is additive (new tables/columns with safe
defaults) except the eventual `projects.team_id NOT NULL` tightening in
step 1 — that one gets its own explicit follow-up migration only after
confirming zero `NULL` rows remain, never bundled with the column's
initial addition.
