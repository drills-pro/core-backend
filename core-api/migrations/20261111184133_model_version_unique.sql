-- migrate:up
-- One row per MLflow version: a retried register (or a double settle) must not index the same version twice (audit C-14).
-- No two rows share a version today (versions come 1:1 from runs), so the constraint adds cleanly.
ALTER TABLE model_versions
  ADD CONSTRAINT model_versions_mlflow_version_key UNIQUE (project_id, mlflow_model_name, mlflow_version);

-- migrate:down
ALTER TABLE model_versions DROP CONSTRAINT model_versions_mlflow_version_key;
