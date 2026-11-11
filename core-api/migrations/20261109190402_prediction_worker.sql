-- migrate:up
-- What the prediction worker and the retention purge read across tenants (planning/24), SECURITY DEFINER like the sibling
-- inflight_*_tenant_ids(): the sweep has no tenant yet, then works each one under TenantConn.
CREATE INDEX predictions_inflight_idx ON predictions(tenant_id) WHERE mode = 'background' AND status IN ('queued', 'in_progress');
CREATE INDEX predictions_expires_idx ON predictions(expires_at) WHERE purged_at IS NULL AND expires_at IS NOT NULL;

CREATE FUNCTION inflight_prediction_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT DISTINCT tenant_id FROM predictions WHERE mode = 'background' AND status IN ('queued', 'in_progress');
$$;
REVOKE ALL ON FUNCTION inflight_prediction_tenant_ids() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION inflight_prediction_tenant_ids() TO core_app;

-- Retention: a sync row past its 24 h goes with its events (the Idempotency-Key lapses with it); a background row keeps id, status,
-- error and its timestamps and loses input, output and events, and says so in purged_at. Returns how many rows it touched.
CREATE FUNCTION purge_predictions() RETURNS bigint
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH dropped AS (DELETE FROM predictions WHERE mode = 'sync' AND expires_at < now() RETURNING 1),
       scrubbed AS (
         UPDATE predictions SET input = NULL, output = NULL, purged_at = now()
         WHERE mode = 'background' AND expires_at < now() AND purged_at IS NULL RETURNING id),
       events AS (DELETE FROM prediction_events WHERE prediction_id IN (SELECT id FROM scrubbed) RETURNING 1)
  SELECT (SELECT count(*) FROM dropped) + (SELECT count(*) FROM scrubbed);
$$;
REVOKE ALL ON FUNCTION purge_predictions() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION purge_predictions() TO core_app;

-- migrate:down
DROP FUNCTION purge_predictions();
DROP FUNCTION inflight_prediction_tenant_ids();
DROP INDEX predictions_expires_idx;
DROP INDEX predictions_inflight_idx;
