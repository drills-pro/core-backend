-- migrate:up
-- Webhook delivery of the events outbox (ADR-005). An endpoint keeps its own cursor into `events` (last_seq), so a
-- delivery is "send what is after the cursor, then move it": at least once, in order, per endpoint. The signing secret
-- is not stored: it is HMAC(master key from the webhooks secret file, endpoint id), so the database holds no secret.
CREATE TABLE webhook_endpoints (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id),
  project_id      uuid NOT NULL REFERENCES projects(id),
  url             text NOT NULL,
  created_by      text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  disabled_at     timestamptz,
  last_seq        bigint NOT NULL DEFAULT 0,
  failures        integer NOT NULL DEFAULT 0 CHECK (failures >= 0),
  next_attempt_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX webhook_endpoints_project_idx ON webhook_endpoints(project_id);
ALTER TABLE webhook_endpoints ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON webhook_endpoints USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT, UPDATE ON webhook_endpoints TO core_app;

-- The dispatcher runs across tenants, before any tenant is set: the tenants with an endpoint that is due and has
-- events to send. Ids only, like the other cross-tenant reads.
CREATE FUNCTION webhook_tenants_due() RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT DISTINCT w.tenant_id FROM webhook_endpoints w
  WHERE w.disabled_at IS NULL AND w.next_attempt_at <= now()
    AND EXISTS (SELECT 1 FROM events e WHERE e.project_id = w.project_id AND e.seq > w.last_seq)
$$;
REVOKE ALL ON FUNCTION webhook_tenants_due() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION webhook_tenants_due() TO core_app;

-- migrate:down
DROP FUNCTION webhook_tenants_due();
DROP TABLE webhook_endpoints;
