# UI revamp — information architecture for a multipage product

## Why Phase 1's UI needs this
Phase 1's frontend is one route (`/projects/$projectId`) with four tabs
(Datasets/Training/Models/Deployments) that each grew a dense form +
table as features landed feature-by-feature within a single sitting —
functional and fully wired to real endpoints, but flat: no org/team
context, no settings surface, no distinction between "quick action" and
"configuration," and several tabs (Training in particular) accumulated
5+ inline form fields in one `HStack` as backends were added. This doc is
the page map Phase 2's new features (02, 06, 07, 08) need, plus the
navigation restructuring current tabs require once a project is no
longer the top-level thing in the URL.

## Page map
```
/                          — org switcher (if >1 org) → team list
/t/:teamId                 — team dashboard: projects grid, team usage summary
/t/:teamId/settings        — team settings: members, invites
/p/:projectId              — project overview: task_type badge, lineage summary, quick actions
/p/:projectId/datasets     — dataset list (+ per-dataset detail drawer, not a full tab reload)
/p/:projectId/datasets/:id — dataset detail: preview, versions, labeling status, preprocessing
/p/:projectId/training     — run list + "new run" as a modal/side-panel, not inline form
/p/:projectId/models       — lineage-strip view per model version (05)
/p/:projectId/deployments  — deployment list + per-deployment "test" panel (06 FR6/FR7)
/p/:projectId/settings     — compute target, base model, deploy strategy, visibility (06, 08)
/p/:projectId/members      — invites, roles (08)
/p/:projectId/usage        — project usage (09)
/settings/profile          — user profile, linked OAuth accounts (07)
/explore                   — public projects/datasets/models directory (08)
/login, /register          — existing, unchanged
```

## Concrete UI debt to pay down (not new features, just current-file cleanup)
- `projects.$projectId.tsx` is one ~900-line file holding every tab's
  component. Splitting by route (above) is also splitting by file —
  each route gets its own file under `src/routes/`, consistent with
  TanStack Router's file-based convention already in use.
- The Training tab's submit form (dataset picker, version picker, test
  dataset picker, test version picker, backend picker, task picker,
  epochs, test split, HF fields) is 9 controls in a flat `HStack`.
  Phase 2 groups it as a 3-step disclosure: **1) What data** (dataset +
  version + test set), **2) What model** (backend + task/base-model,
  defaulted from project settings per 06 FR3), **3) How long** (epochs +
  advanced/HF fields collapsed by default) — reduces first-glance choice
  count without removing any capability.
- Confusion-matrix/epoch-chart detail (already built, real SVG chart) is
  print for every backend, hand-styled and correct — reuse as the
  chart component in the new Models tab lineage view (05) instead of a
  new chart implementation.

## Component library
Keep Chakra UI v3 — Phase 1 is already fully built on it, real-work cost
of a swap (design tokens, every existing component) isn't justified by
this phase's goals. New pages compose from the same primitives
(`Tabs`, `Table`, `NativeSelect`, `SimpleGrid`) already proven out.

## Design direction (not a full design system spec)
- Nav becomes two-level: team switcher + project switcher, persistent
  sidebar rather than Phase 1's single top nav bar (which has no room
  for team context).
- Every list page (datasets, runs, models, deployments) gets a
  consistent row-status-badge + row-detail-drawer pattern instead of
  each tab inventing its own (Phase 1's confusion-matrix expand-row vs.
  dataset-preview-inline-gallery are two different patterns for
  "show me more about this row" today).
