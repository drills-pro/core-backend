-- Deployments (req/04 Deployments group, req/07 FR3). Run after 0006.

ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS model_artifact jsonb;

CREATE TABLE IF NOT EXISTS deployments (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES tenants(id),
  project_id       uuid NOT NULL REFERENCES projects(id),
  training_run_id  uuid NOT NULL REFERENCES training_runs(id),
  ray_job_id       text NOT NULL,
  route_prefix     text NOT NULL,
  status           text NOT NULL DEFAULT 'deploying'
                     CHECK (status IN ('deploying', 'active', 'failed', 'deleted')),
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS deployments_project_idx ON deployments(project_id, created_at DESC);

ALTER TABLE deployments ENABLE ROW LEVEL SECURITY;
ALTER TABLE deployments FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON deployments;
CREATE POLICY tenant_isolation ON deployments
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE ON deployments TO core_app;
