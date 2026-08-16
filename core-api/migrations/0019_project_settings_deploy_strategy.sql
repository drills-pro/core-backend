-- Project settings and deployment strategy
-- (improvement/phase2/06-project-settings-deployment.md). Additive per
-- 11-migration-phasing.md step 3.

-- FR1: compute_target / base_model / deploy_strategy, lean jsonb since
-- nothing here needs to be individually indexed.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS settings jsonb NOT NULL DEFAULT '{}'::jsonb;

-- FR7: last-N test invocations for the in-app "try it" panel.
CREATE TABLE IF NOT EXISTS deployment_test_calls (
  id            bigserial PRIMARY KEY,
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  deployment_id uuid NOT NULL REFERENCES deployments(id),
  request       jsonb NOT NULL,
  response       jsonb,
  latency_ms    numeric,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS deployment_test_calls_deployment_idx
  ON deployment_test_calls(deployment_id, created_at DESC);

ALTER TABLE deployment_test_calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE deployment_test_calls FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON deployment_test_calls;
CREATE POLICY tenant_isolation ON deployment_test_calls
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT ON deployment_test_calls TO core_app;
GRANT USAGE ON SEQUENCE deployment_test_calls_id_seq TO core_app;
