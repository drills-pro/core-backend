-- migrate:up
-- Workstream "deploy and promote" (planning/27): promoting a version to Production deploys it, asynchronously; the console's try-it
-- is a real stored prediction.

-- A promotion in flight or failed, on the version being promoted. The stage itself flips only when the deployment is active, so a
-- failed promotion leaves every stage as it was and its error here. One pending promotion per model at a time.
ALTER TABLE model_versions
  ADD COLUMN promotion_status text CONSTRAINT model_versions_promotion_status_check CHECK (promotion_status IN ('pending', 'failed')),
  ADD COLUMN promotion_error text,
  ADD COLUMN promotion_error_code text,  -- the problem code of promotion_error, so a client can branch (deploy_risk_unacknowledged asks for an acknowledgement)
  ADD COLUMN promotion_actor text,
  ADD COLUMN promotion_acknowledged_risk boolean NOT NULL DEFAULT false,
  ADD COLUMN promotion_requested_at timestamptz;
CREATE UNIQUE INDEX model_versions_one_pending_per_model ON model_versions(model_id) WHERE promotion_status = 'pending';

-- The worker's cross-tenant listing: tenants with a pending promotion (active tenants only, like the other sweeps).
CREATE FUNCTION pending_promotion_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT DISTINCT v.tenant_id FROM model_versions v JOIN tenants t ON t.id = v.tenant_id AND t.status = 'active'
  WHERE v.promotion_status = 'pending';
$$;
REVOKE ALL ON FUNCTION pending_promotion_tenant_ids() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION pending_promotion_tenant_ids() TO core_app;

-- A prediction made from the console by a signed-in member has no API key. `source` says which door it came through.
ALTER TABLE predictions ALTER COLUMN api_key_id DROP NOT NULL;
ALTER TABLE predictions ADD COLUMN source text NOT NULL DEFAULT 'api' CONSTRAINT predictions_source_check CHECK (source IN ('api', 'console'));
ALTER TABLE predictions ADD CONSTRAINT predictions_caller_check CHECK ((source = 'api') = (api_key_id IS NOT NULL));
-- The try-it log of a deployment: its console predictions, newest first.
CREATE INDEX predictions_console_idx ON predictions(deployment_id, created_at DESC) WHERE source = 'console';

-- migrate:down
DROP INDEX predictions_console_idx;
ALTER TABLE predictions DROP CONSTRAINT predictions_caller_check;
DELETE FROM predictions WHERE source = 'console';
ALTER TABLE predictions DROP COLUMN source;
ALTER TABLE predictions ALTER COLUMN api_key_id SET NOT NULL;
DROP FUNCTION pending_promotion_tenant_ids();
DROP INDEX model_versions_one_pending_per_model;
ALTER TABLE model_versions
  DROP COLUMN promotion_requested_at, DROP COLUMN promotion_acknowledged_risk, DROP COLUMN promotion_actor,
  DROP COLUMN promotion_error_code, DROP COLUMN promotion_error, DROP COLUMN promotion_status;
