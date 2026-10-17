-- migrate:up
-- Training run orchestration (req/06, req/07). Run after 0003.

CREATE TABLE IF NOT EXISTS training_runs (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id),
  project_id      uuid NOT NULL REFERENCES projects(id),
  idempotency_key text NOT NULL,
  ray_job_id      text NOT NULL,
  mlflow_run_id   text,
  dataset_ref     text NOT NULL,
  status          text NOT NULL DEFAULT 'submitted'
                    CHECK (status IN ('submitted', 'running', 'succeeded', 'failed', 'cancelled')),
  created_at      timestamptz NOT NULL DEFAULT now(),
  completed_at    timestamptz,
  -- FR2 (04-api.md): idempotency key on training-run creation — a client
  -- retry with the same key must return the existing run, not submit twice.
  UNIQUE (project_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS training_runs_tenant_idx ON training_runs(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS training_runs_project_idx ON training_runs(project_id, created_at DESC);
-- FR4 (07-compute-ray-k8s.md): count in-flight runs per tenant for the
-- hard-quota check before submission.
CREATE INDEX IF NOT EXISTS training_runs_inflight_idx ON training_runs(tenant_id)
  WHERE status IN ('submitted', 'running');

ALTER TABLE training_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE training_runs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON training_runs;
CREATE POLICY tenant_isolation ON training_runs
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE ON training_runs TO core_app;

-- migrate:down
DROP TABLE IF EXISTS training_runs;
