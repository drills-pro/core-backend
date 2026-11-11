-- migrate:up
-- Stored predictions (planning/24): a background prediction, and a sync one sent with an Idempotency-Key (kept 24 h so a retry
-- gets the original). `status` is the PredictionStatus enum; `mode` says who owns the row (the request handler for sync, the
-- prediction worker for background). attempt / lease_* / next_attempt_at are the worker's bookkeeping; every write-back is fenced
-- on `attempt`, so a worker that lost its lease cannot overwrite the current one.
CREATE TABLE predictions (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES tenants(id),
  project_id       uuid NOT NULL REFERENCES projects(id),
  api_key_id       uuid NOT NULL REFERENCES api_keys(id),
  deployment_id    uuid NOT NULL REFERENCES deployments(id),
  model_id         uuid NOT NULL REFERENCES models(id),
  model_revision   text NOT NULL,
  mode             text NOT NULL CONSTRAINT predictions_mode_check CHECK (mode IN ('sync', 'background')),
  status           text NOT NULL CONSTRAINT predictions_status_check
                     CHECK (status IN ('queued', 'in_progress', 'completed', 'failed', 'cancelled')),
  cancel_requested boolean NOT NULL DEFAULT false,
  input            jsonb,
  output           jsonb,
  error            jsonb,
  metadata         jsonb NOT NULL DEFAULT '{}',
  latency_ms       double precision,
  idempotency_key  text,
  request_hash     text,
  attempt          integer NOT NULL DEFAULT 0,
  max_attempts     integer NOT NULL,
  lease_owner      text,
  lease_expires_at timestamptz,
  next_attempt_at  timestamptz NOT NULL DEFAULT now(),
  created_at       timestamptz NOT NULL DEFAULT now(),
  started_at       timestamptz,
  finished_at      timestamptz,
  expires_at       timestamptz,
  purged_at        timestamptz
);
-- The idempotency scope: one key per project, shared by its API keys.
CREATE UNIQUE INDEX predictions_idempotency_idx ON predictions(project_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX predictions_owner_idx ON predictions(project_id, api_key_id, created_at DESC);

ALTER TABLE predictions ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON predictions USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT, UPDATE ON predictions TO core_app;

-- The replay log of GET /v1/predict/{id}/events: one row per status change, written in the transaction of the change.
CREATE TABLE prediction_events (
  prediction_id uuid NOT NULL REFERENCES predictions(id) ON DELETE CASCADE,
  seq           integer NOT NULL,
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  status        text NOT NULL CONSTRAINT prediction_events_status_check
                  CHECK (status IN ('queued', 'in_progress', 'completed', 'failed', 'cancelled')),
  data          jsonb NOT NULL DEFAULT '{}',
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (prediction_id, seq)
);
ALTER TABLE prediction_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON prediction_events USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT ON prediction_events TO core_app;

-- migrate:down
DROP TABLE prediction_events;
DROP TABLE predictions;
