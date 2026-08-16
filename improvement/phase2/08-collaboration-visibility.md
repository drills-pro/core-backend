# Collaboration and visibility — invites, public/private

## Invites (extends existing project_members)
FR1: `POST /v1/projects/{id}/invites` — email + role
(`org_owner`/`org_admin`/`project_editor`/`project_viewer`, the same
vocabulary 02 reuses from `scripts/zitadel-bootstrap.sh`). Backed by a
new `project_invites` table (`email, project_id, role, invited_by,
status, token, expires_at`) — not a Zitadel-side concept, since an
invitee may not have a Zitadel user yet.
FR2: Invite acceptance: if the invited email already has a platform
account, the invite becomes a `project_members` row immediately on
accept. If not, acceptance flow is register (07's normal signup) → then
consume the pending invite by matching verified email — same
email-is-the-anchor principle as 07, applied to invites instead of OAuth.
FR3: Email delivery: this stack has no outbound email configured
anywhere yet (Phase 1's `register` endpoint marks email `isVerified: True`
specifically *because* no SMTP exists — see `app/embedded_auth.py`'s own
comment). Phase 2 needs a real transactional email path for invites to
be usable — smallest addition that satisfies it without a new service:
Zitadel itself can send notification emails if given SMTP config
(it already has a notification subsystem, just unconfigured) — reuse
that rather than adding a separate email-sending library/service to
core-api.

## Public / private projects
FR4: `projects.visibility` (`private | public`), owner-toggleable.
FR5: A public project is discoverable (a new unauthenticated
`GET /v1/public/projects` listing) but **owner explicitly chooses what's
public within it** — three independent flags, not one:
`publish_project_metadata` (name/description/task_type),
`publish_dataset` (per-dataset, default off), `publish_model` (per-model
version, default off). A public project with no publish flags set shows
only that it exists ("a public image_classification project by
<owner>") — this is the literal ask: *"if dataset is private just mark
it as private without name."* Applies symmetrically to datasets: a
private dataset inside a public project is listed as "1 private dataset"
with no name/preview leaked.
FR6: Publishing a dataset publishes its *current LakeFS commit* at
publish time (a real, addressable version — consistent with 05's
lineage principle), not a live-updating pointer; a new commit doesn't
become public until re-published, so an owner can't accidentally leak an
in-progress upload.
FR7: Publishing a model publishes the MLflow registry entry's metadata
(metrics, config) and optionally the ONNX/weights artifact — two
separate toggles, since "let people see my model got 94% accuracy" and
"let people download my trained weights" are very different disclosures.

## RLS implication
FR8: Public read paths (`GET /v1/public/...`) are the one place Phase 2
deliberately bypasses the tenant-RLS `SELECT` policy — same
`SECURITY DEFINER` bypass-function pattern already established for
cross-tenant existence checks (`project_exists_any_tenant`,
`inflight_training_tenant_ids`, etc. in the migrations), scoped tightly
to `WHERE visibility = 'public' AND publish_* = true`, never a blanket
bypass.
