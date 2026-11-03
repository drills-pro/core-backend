-- migrate:up
-- One row per key-authenticated /predict call that reached the shared predictor apps: what was asked of which run, how it
-- went and how long it took. No request or response bodies. Append-only for core_app; the purge below is the only delete.
CREATE TABLE predict_calls (
  id              bigserial PRIMARY KEY,
  tenant_id       uuid NOT NULL REFERENCES tenants(id),
  deployment_id   uuid NOT NULL REFERENCES deployments(id),
  training_run_id uuid NOT NULL REFERENCES training_runs(id),
  status          integer NOT NULL,  -- the HTTP status the caller got
  latency_ms      numeric NOT NULL,
  cold_load       boolean,           -- null when the call failed before the model answered
  error_code      text CONSTRAINT predict_calls_error_code_check CHECK (error_code IN (
                    'model_load_failed', 'unknown_function', 'bad_input', 'serve_unreachable', 'serve_timeout', 'serve_bad_answer')),
  request_bytes   integer NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX predict_calls_deployment_idx ON predict_calls(deployment_id, created_at DESC);
CREATE INDEX predict_calls_created_idx ON predict_calls(created_at);

ALTER TABLE predict_calls ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON predict_calls USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT ON predict_calls TO core_app;
GRANT USAGE ON SEQUENCE predict_calls_id_seq TO core_app;

-- Across tenants, so SECURITY DEFINER like the other purges: drops what is older than p_days and says how many.
CREATE FUNCTION purge_predict_calls(p_days integer) RETURNS bigint
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH gone AS (DELETE FROM predict_calls WHERE created_at < now() - make_interval(days => p_days) RETURNING 1)
  SELECT count(*) FROM gone;
$$;
REVOKE ALL ON FUNCTION purge_predict_calls(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION purge_predict_calls(integer) TO core_app;

-- migrate:down
DROP FUNCTION purge_predict_calls(integer);
DROP TABLE predict_calls;
