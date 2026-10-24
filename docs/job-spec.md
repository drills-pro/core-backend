# Job spec

The one document core-api hands every Ray job. It generalises the training-only "Job spec v1" of
`planning/06`: an **envelope** shared by every job plus a **payload** chosen by `kind`.

| Part | Content |
|---|---|
| Envelope | `spec_version`, `kind`, `job_id` (the submission id), `attempt`, `tenant_id`, `resources` (cpu, memory, gpu, max runtime; declared, never inferred), optional `reporting` (events URL, token env var name) |
| `kind=ingest` | what the dataset import entrypoints read: staging `bucket` and `prefix`, `source`, `task`, `target`, `split`, `limits`, and when relevant `base` (presigned reads of the base version) and `hf` |
| `kind=train` | task and preset, pinned base model, data pinned by lakeFS commit, hyperparameters, tracking, output (not run by anything yet) |

Rules: secrets are never in the document (the staging credential is env vars set by the submitter); a job refuses a
`spec_version` or `kind` it does not know; a change to a field a running job reads is a new `spec_version`.

Code: models in `core-api/app/integrations/job_spec.py`, JSON Schema in `contracts/job-spec.v1.json` (rewrite it with
`scripts/gen-job-spec-schema.py`; a test fails when it is stale), reader in `ray-jobs/ray_jobs/spec.py`.
`tests/ingest_job/test_contract.py` checks the job reads what core-api writes.

**Adding a job kind** (dataset preparation, say): a payload model and a `JobSpec` union member in `job_spec.py`, a
`JobKind` member, entrypoint modules in `ray-jobs/ray_jobs/entrypoints/`, entries in `data_plane/job_catalog.py`.
Nothing in the envelope, the runner, the shipping or the catalog changes. How the spec relates to the other two
"job spec" types: `job_catalog.CatalogEntry` says which module runs a job type; `compute.JobSpec` (RayJob branch) says
how a submission is made. This document says what the job does.
