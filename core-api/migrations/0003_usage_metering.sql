-- Usage metering (req/09-usage-metering.md). Run after 0002.

-- Append-only ledger. UNIQUE(resource_type, source_ref) is the idempotency
-- key from FR1/NFR2 — a retried job-completion callback / snapshot job /
-- rollup job just no-ops on conflict instead of double-billing.
CREATE TABLE IF NOT EXISTS usage_events (
  id              bigserial PRIMARY KEY,
  tenant_id       uuid NOT NULL REFERENCES tenants(id),
  project_id      uuid NOT NULL REFERENCES projects(id),
  resource_type   text NOT NULL CHECK (resource_type IN ('training', 'storage_dataset', 'storage_model', 'inference')),
  quantity        numeric NOT NULL CHECK (quantity >= 0),
  unit            text NOT NULL,
  occurred_at     timestamptz NOT NULL,
  source_ref      text NOT NULL,
  pricing_version text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (resource_type, source_ref)
);

CREATE INDEX IF NOT EXISTS usage_events_tenant_idx ON usage_events(tenant_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS usage_events_project_idx ON usage_events(project_id, occurred_at DESC);

ALTER TABLE usage_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON usage_events;
CREATE POLICY tenant_isolation ON usage_events
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- NFR1: durable, replayable ledger — INSERT only, no UPDATE/DELETE.
-- Corrections are new offsetting rows per the doc, not edits.
GRANT SELECT, INSERT ON usage_events TO core_app;
GRANT USAGE ON SEQUENCE usage_events_id_seq TO core_app;
