-- migrate:up
-- X-Model-Id needs a stable identity (planning/24, Q1): one models row per (project, registered MLflow name), which
-- model_versions reference. The MLflow name is stored as is, so the registry naming is untouched; X-Model-Revision is the
-- version's number (mlflow_version).
CREATE TABLE models (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id  uuid NOT NULL REFERENCES tenants(id),
  project_id uuid NOT NULL REFERENCES projects(id),
  name       text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, name)
);
ALTER TABLE models ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON models USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT, UPDATE ON models TO core_app;

INSERT INTO models (tenant_id, project_id, name, created_at)
SELECT tenant_id, project_id, mlflow_model_name, min(created_at) FROM model_versions GROUP BY tenant_id, project_id, mlflow_model_name;

ALTER TABLE model_versions ADD COLUMN model_id uuid REFERENCES models(id);
UPDATE model_versions v SET model_id = m.id FROM models m WHERE m.project_id = v.project_id AND m.name = v.mlflow_model_name;
ALTER TABLE model_versions ALTER COLUMN model_id SET NOT NULL;
CREATE INDEX model_versions_model_id_idx ON model_versions(model_id, created_at DESC);

-- A deployment serves one model version. Rows made before this column take the newest version registered from their run.
ALTER TABLE deployments ADD COLUMN model_version_id uuid REFERENCES model_versions(id);
UPDATE deployments d SET model_version_id = (
  SELECT v.id FROM model_versions v WHERE v.training_run_id = d.training_run_id ORDER BY v.created_at DESC, v.id LIMIT 1);
CREATE INDEX deployments_model_version_idx ON deployments(model_version_id);
-- deploying is included so two concurrent creates cannot both pass; stopped is not (it already holds the run through
-- deployments_one_live_per_run, which is the stricter rule while versions are 1:1 with runs).
CREATE UNIQUE INDEX deployments_one_active_per_version ON deployments(model_version_id) WHERE status IN ('deploying', 'active');

-- api_key_models.model_id was a bare uuid until models existed. Rows written before this may name nothing, and deleting
-- them would widen a key from "these models" to "all models", so the constraint checks new rows always and old rows only
-- when none is dangling.
ALTER TABLE api_key_models ADD CONSTRAINT api_key_models_model_fk FOREIGN KEY (model_id) REFERENCES models(id) NOT VALID;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM api_key_models k WHERE NOT EXISTS (SELECT 1 FROM models m WHERE m.id = k.model_id)) THEN
    ALTER TABLE api_key_models VALIDATE CONSTRAINT api_key_models_model_fk;
  END IF;
END
$$;

-- migrate:down
ALTER TABLE api_key_models DROP CONSTRAINT api_key_models_model_fk;
DROP INDEX deployments_one_active_per_version;
DROP INDEX deployments_model_version_idx;
ALTER TABLE deployments DROP COLUMN model_version_id;
DROP INDEX model_versions_model_id_idx;
ALTER TABLE model_versions DROP COLUMN model_id;
DROP TABLE models;
