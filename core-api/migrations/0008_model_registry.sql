-- Model registry pointer (req/04 Models group, req/06 FR2). MLflow's
-- Model Registry has no tenant concept, so this table is the tenant-scoped
-- index onto it — the RLS boundary, not a duplicate of MLflow's data.
CREATE TABLE IF NOT EXISTS model_versions (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id          uuid NOT NULL REFERENCES tenants(id),
  project_id         uuid NOT NULL REFERENCES projects(id),
  training_run_id    uuid NOT NULL REFERENCES training_runs(id),
  mlflow_model_name  text NOT NULL,
  mlflow_version      text NOT NULL,
  stage              text NOT NULL DEFAULT 'None',
  created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS model_versions_project_idx ON model_versions(project_id, created_at DESC);

ALTER TABLE model_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE model_versions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON model_versions;
CREATE POLICY tenant_isolation ON model_versions
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE ON model_versions TO core_app;
