-- migrate:up
-- Legacy leftovers with no reader or writer left: the try-it log reads predictions now, so the table that the
-- old console wrote is cut with its purge; the serving marker (every row is shared) and the deploying_since
-- stamp that only the retired legacy serving set go too.

DROP FUNCTION purge_test_calls(integer);

-- migration-lint: allow-destructive (cut for now - the try-it log reads predictions, nothing writes this table)
DROP TABLE deployment_test_calls;

-- migration-lint: allow-destructive (cut for now - every deployment is shared, nothing branches on the marker)
ALTER TABLE deployments DROP COLUMN serving;

-- migration-lint: allow-destructive (cut for now - nothing reads the stamp)
ALTER TABLE deployments DROP COLUMN deploying_since;

-- migrate:down
-- Restores the columns, the try-it table with its indexes, grants and row policy, and its purge function.
ALTER TABLE deployments
  ADD COLUMN serving text NOT NULL DEFAULT 'legacy'
    CONSTRAINT deployments_serving_check CHECK (serving IN ('legacy', 'shared')),
  ADD COLUMN deploying_since timestamptz NOT NULL DEFAULT now();

CREATE TABLE deployment_test_calls (
  id            bigserial PRIMARY KEY,
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  deployment_id uuid NOT NULL REFERENCES deployments(id),
  request       jsonb NOT NULL,
  response      jsonb,
  latency_ms    numeric,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX deployment_test_calls_deployment_idx ON deployment_test_calls(deployment_id, created_at DESC);
CREATE INDEX deployment_test_calls_created_idx ON deployment_test_calls(created_at);
ALTER TABLE deployment_test_calls ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON deployment_test_calls USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT ON deployment_test_calls TO core_app;
GRANT USAGE ON SEQUENCE deployment_test_calls_id_seq TO core_app;

CREATE FUNCTION purge_test_calls(p_days integer) RETURNS bigint
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH gone AS (DELETE FROM deployment_test_calls WHERE created_at < now() - make_interval(days => p_days) RETURNING 1)
  SELECT count(*) FROM gone;
$$;
REVOKE ALL ON FUNCTION purge_test_calls(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION purge_test_calls(integer) TO core_app;


