-- Tracks background Ray jobs that materialize a Hugging Face Hub dataset
-- into a platform dataset's own S3 prefix (app/datasets.py's import-hf
-- endpoint) — same submitted/running/succeeded/failed shape as
-- training_runs, reconciled the same way (app/datasets.py's
-- reconcile_inflight_imports).
CREATE TABLE IF NOT EXISTS dataset_imports (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id),
  project_id  uuid NOT NULL REFERENCES projects(id),
  dataset_id  uuid NOT NULL REFERENCES datasets(id),
  ray_job_id  text NOT NULL,
  hf_dataset  text NOT NULL,
  kind        text NOT NULL CHECK (kind IN ('images', 'tabular')),
  status      text NOT NULL DEFAULT 'submitted'
                CHECK (status IN ('submitted', 'running', 'succeeded', 'failed')),
  error       text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);

CREATE INDEX IF NOT EXISTS dataset_imports_dataset_idx ON dataset_imports(dataset_id, created_at DESC);
CREATE INDEX IF NOT EXISTS dataset_imports_inflight_idx ON dataset_imports(tenant_id)
  WHERE status IN ('submitted', 'running');

ALTER TABLE dataset_imports ENABLE ROW LEVEL SECURITY;
ALTER TABLE dataset_imports FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON dataset_imports;
CREATE POLICY tenant_isolation ON dataset_imports
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE ON dataset_imports TO core_app;

-- Same SECURITY DEFINER bypass pattern as inflight_training_tenant_ids
-- (0011) — background reconciliation needs in-flight imports across all
-- tenants without an app.tenant_id already set.
CREATE OR REPLACE FUNCTION inflight_import_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT DISTINCT tenant_id FROM dataset_imports WHERE status IN ('submitted', 'running');
$$;
REVOKE ALL ON FUNCTION inflight_import_tenant_ids() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION inflight_import_tenant_ids() TO core_app;
