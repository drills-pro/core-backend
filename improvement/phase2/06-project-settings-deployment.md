# Project settings and deployment strategy

## Bug fixed during Phase 1 close-out (context, not a Phase 2 task)
Deployments were getting stuck at `status = "deploying"` forever. Root
cause: `list_deployments` never re-polled Ray for status (only a
single-deployment `GET` did, and nothing ever called it for a deployment
sitting in a list view) — the exact same missing-reconciler bug pattern
training runs and dataset imports each independently hit earlier in
Phase 1, before "every async state machine gets a reconciler" became the
standing rule (00). Fixed by adding `reconcile_inflight_deployments`
(same shape as the other two) plus the RLS-bypass helper migration it
needs. Not a GPU-multiplexing problem — Ray Serve already runs multiple
deployments concurrently on one cluster fine; the deployments really had
finished, nothing ever told Postgres.

## New: project-level deployment settings
FR1: `projects` gains a settings sub-object (either a `jsonb` column or a
`project_settings` table — lean `jsonb` unless a field needs to be
individually queried/indexed):
```json
{
  "compute_target": "cpu | gpu",
  "base_model": "<backend-specific default, e.g. yolo11n vs yolo11n.pt vs a HF model id>",
  "deploy_strategy": "main | latest"
}
```
FR2: `compute_target` maps to a Ray resource request
(`num_gpus: 1` vs `num_gpus: 0`) on both the training job submission
(`ray_client.submit_job`) and the serve deployment (`serve_client.deploy`)
— currently both are hardcoded CPU-only. This is the one place Phase 2
touches the Ray job-submission payload shape, not the template scripts
themselves.
FR3: `base_model` is the project-level default fed into the training
form's "base model" picker (already built per-run in Phase 1's Training
tab) — reduces "pick Ultralytics + classify + yolo11n-cls every single
run" to "confirm the project default."
FR4: `deploy_strategy`:
  - `main`: deploying a model updates one stable route
    (`/predict/{project_id}/main`) in place — the *route* is what
    downstream integrators point at permanently; which model backs it
    changes over time. Requires Ray Serve's existing support for
    updating a running deployment's replica (`serve.run` against the
    same `name` again, which Phase 1's `serve_client.deploy` already
    does per-deployment — the new part is *reusing* the same route name
    across deploys instead of minting a new UUID route every time).
  - `latest`: today's Phase 1 behavior — every deploy gets its own
    permanent route (`/predict/{deployment_id}`), old ones keep serving
    until explicitly torn down.
FR5: Switching an active `main`-strategy project's model is then a single
action from the Models tab lineage view (05) — "promote this version to
main" — rather than "create a new deployment and go update every
downstream caller's URL."

## Deployment metadata for testing (screenshot request: "test with metadata")
FR6: The `/predict/{id}` call gains an optional `metadata` passthrough
object in the request body, echoed back in the response alongside the
prediction — lets a UI "test this deployment" panel show request/response
pairs with a label ("test case 3") without the backend needing to
understand what the label means. Paired with:
FR7: A `POST .../deployments/{id}/test` endpoint — same auth as the
management API (tenant JWT, not predict-key), for the in-app "try it"
panel so a project owner can smoke-test a deployment without needing the
predict key at all. Stores the last N test invocations
(`deployment_test_calls` table: `deployment_id, request, response,
latency_ms, created_at`) so the UI can show a running log, not just the
latest call.
