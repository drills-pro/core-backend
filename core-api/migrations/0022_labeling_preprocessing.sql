-- Labeling and preprocessing pipelines
-- (improvement/phase2/04-labeling-preprocessing.md). Additive per
-- 11-migration-phasing.md step 6, ordered last since it depends on 03's
-- task_type gate existing.

-- FR4-FR6: same shape as dataset_imports (0013) — a Ray job whose output
-- is a new LakeFS commit.
CREATE TABLE IF NOT EXISTS dataset_preprocessing_jobs (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  project_id    uuid NOT NULL REFERENCES projects(id),
  dataset_id    uuid NOT NULL REFERENCES datasets(id),
  ray_job_id    text NOT NULL,
  config        jsonb NOT NULL,
  status        text NOT NULL DEFAULT 'submitted'
                CHECK (status IN ('submitted', 'running', 'succeeded', 'failed')),
  output_ref    text,
  error         text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  completed_at  timestamptz
);

CREATE INDEX IF NOT EXISTS dataset_preprocessing_jobs_dataset_idx
  ON dataset_preprocessing_jobs(dataset_id, created_at DESC);

ALTER TABLE dataset_preprocessing_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE dataset_preprocessing_jobs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON dataset_preprocessing_jobs;
CREATE POLICY tenant_isolation ON dataset_preprocessing_jobs
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE ON dataset_preprocessing_jobs TO core_app;

-- Same SECURITY DEFINER cross-tenant-sweep pattern as
-- inflight_import_tenant_ids (0013)/inflight_deploy_tenant_ids (0016).
CREATE OR REPLACE FUNCTION inflight_preprocessing_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT DISTINCT tenant_id FROM dataset_preprocessing_jobs WHERE status IN ('submitted', 'running');
$$;
REVOKE ALL ON FUNCTION inflight_preprocessing_tenant_ids() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION inflight_preprocessing_tenant_ids() TO core_app;
