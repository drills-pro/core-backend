-- migrate:up
-- Live progress of a training run (audit C-07). The trainer posts one `progress` event per epoch with a per-run token;
-- only the sha256 of that token is stored, on the run. One row per (run, seq), so a repeated post changes nothing.
ALTER TABLE training_runs ADD COLUMN events_token_hash text;  -- null: the run has no events channel

CREATE TABLE training_run_events (
  run_id     uuid NOT NULL REFERENCES training_runs(id) ON DELETE CASCADE,
  seq        integer NOT NULL CONSTRAINT training_run_events_seq_check CHECK (seq >= 0),
  tenant_id  uuid NOT NULL REFERENCES tenants(id),
  type       text NOT NULL CONSTRAINT training_run_events_type_check CHECK (type IN ('progress')),
  epoch      integer NOT NULL CONSTRAINT training_run_events_epoch_check CHECK (epoch >= 1),
  epochs     integer NOT NULL CONSTRAINT training_run_events_epochs_check CHECK (epochs >= 1),
  metrics    jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, seq)
);

ALTER TABLE training_run_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON training_run_events USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT ON training_run_events TO core_app;

-- The trainer arrives with a run id and a token, before any tenant context exists, so the lookup runs as the owner. It
-- returns the stored hash for the caller to compare in constant time; a run without a channel returns no row.
CREATE FUNCTION training_run_events_auth(p_run_id uuid)
RETURNS TABLE(tenant_id uuid, token_hash text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT r.tenant_id, r.events_token_hash FROM training_runs r WHERE r.id = p_run_id AND r.events_token_hash IS NOT NULL;
$$;
REVOKE ALL ON FUNCTION training_run_events_auth(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION training_run_events_auth(uuid) TO core_app;

-- migrate:down
DROP FUNCTION training_run_events_auth(uuid);
DROP TABLE training_run_events;
ALTER TABLE training_runs DROP COLUMN events_token_hash;
