# Reproducibility and lineage

## The chain (mostly already built)
```
LakeFS dataset commit(s)  →  training_runs row (dataset_version, test_dataset_version, config)
                          →  MLflow run (params, metrics, epoch curves)
                          →  model_versions row (mlflow_model_name, mlflow_version, stage)
                          →  deployments row (training_run_id → walks back to everything above)
```
Every arrow in this chain already exists in Phase 1's schema and code —
`training_runs.dataset_version`/`test_dataset_version` (LakeFS commits),
`training_runs.mlflow_run_id`, `model_versions.training_run_id`,
`deployments.training_run_id`. Phase 2's job is exposing the *whole
chain* as one view, not inventing new links.

FR1: `GET /v1/projects/{id}/training-runs/{run_id}/lineage` — a single
endpoint that joins all four tables above for one run and returns the
full chain (dataset commit message/timestamp, MLflow run URL, registry
stage, active deployment if any). Currently this exists as four separate
lookups a user would have to correlate by hand across tabs.
FR2: The Models tab UI (10) renders this as a compact lineage strip per
model version: `dataset@commit → training run → this model → [deployed / not deployed]`.

## "Reproduce this run" as a real action
FR3: A button on any training run — "Reproduce" — resubmits
`create_training_run` with the exact same `dataset_id`,
`dataset_version`, `test_dataset_id`, `test_dataset_version`, and
`config` as the source run. This is a thin client-side action (the API
already accepts all these fields; Phase 1 just never built the "copy
these fields from an existing row" client call) — proof that the
versioning work already done makes reproduction free once pinning is
consistent.
FR4: Two runs reproduced from the same lineage get a shared
`reproduction_group_id` (new nullable column on `training_runs`) so the
UI can cluster "these 3 runs are the same experiment, different config"
instead of a flat chronological list.

## Model version = MLflow registry entry, Postgres row is a pointer
FR5: Explicitly restated as a constraint (also in 01): `model_versions`
never duplicates data MLflow already tracks (metrics, params, artifact
path) — it stores only `(training_run_id, mlflow_model_name,
mlflow_version, stage)` and the UI reads the rest live from MLflow's API
when a model's detail view is opened. This is already exactly what
`app/models.py` does; Phase 2 changes nothing here except surfacing more
of what MLflow already returns (e.g. registered model description,
tags) in the UI.

## Dataset-side lineage
FR6: A dataset's own version history (`GET .../datasets/{id}/versions`,
already built) gains a reverse-lookup: `GET
.../datasets/{id}/versions/{commit_id}/training-runs` — every run that
was ever pinned to that exact commit. This answers "if I edit/re-commit
this dataset, what previously-reproducible runs am I affecting" before
the user finds out the hard way.
