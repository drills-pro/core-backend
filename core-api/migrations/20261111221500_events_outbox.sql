-- migrate:up
-- The outbox (ADR-005): one row per job state change, written in the transaction that moves the job, so an event exists
-- if and only if the change committed. Clients read it by cursor instead of polling each job; a dispatcher for webhooks
-- would read the same rows.
CREATE TABLE events (
  seq         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id   uuid NOT NULL REFERENCES tenants(id),
  project_id  uuid NOT NULL REFERENCES projects(id),
  kind        text NOT NULL,  -- <job>.<status>
  entity_id   uuid NOT NULL,
  status      text NOT NULL,
  occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX events_project_seq_idx ON events(project_id, seq);
ALTER TABLE events ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON events USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT ON events TO core_app;
GRANT USAGE ON SEQUENCE events_seq_seq TO core_app;

-- migrate:down
DROP TABLE events;
