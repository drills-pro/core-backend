-- Labeling (req/04: "proxy/webhook to Label Studio project, pull labeled
-- export"). One Label Studio project per platform dataset.
CREATE TABLE IF NOT EXISTS labeling_projects (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id),
  project_id        uuid NOT NULL REFERENCES projects(id),
  dataset_id        uuid NOT NULL REFERENCES datasets(id),
  label_studio_id   integer NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS labeling_projects_project_idx ON labeling_projects(project_id, created_at DESC);

ALTER TABLE labeling_projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE labeling_projects FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON labeling_projects;
CREATE POLICY tenant_isolation ON labeling_projects
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT ON labeling_projects TO core_app;
