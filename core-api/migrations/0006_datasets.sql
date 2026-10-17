-- migrate:up
-- Dataset upload (req/04-api.md: "upload returns presigned S3/MinIO URL,
-- client PUTs directly"; req/01: per-tenant object storage prefix).
CREATE TABLE IF NOT EXISTS datasets (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id),
  project_id  uuid NOT NULL REFERENCES projects(id),
  name        text NOT NULL,
  s3_prefix   text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS datasets_project_idx ON datasets(project_id, created_at DESC);

ALTER TABLE datasets ENABLE ROW LEVEL SECURITY;
ALTER TABLE datasets FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON datasets;
CREATE POLICY tenant_isolation ON datasets
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE ON datasets TO core_app;

-- migrate:down
DROP TABLE IF EXISTS datasets;
