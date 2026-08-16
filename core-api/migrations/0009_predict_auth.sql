-- req/04: "Inference (per-tenant) POST /v1/predict/{deployment_id} —
-- separate auth scope from management API — this is what the customer's
-- own downstream systems call." Genuinely separate: a per-deployment
-- secret, not the tenant/JWT credential used everywhere else.
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS predict_key_hash text;

-- SECURITY DEFINER so /predict can look up a deployment by (id, key hash)
-- without ever holding a tenant session context — the caller proves
-- possession of the per-deployment secret instead.
CREATE OR REPLACE FUNCTION deployment_for_predict(p_id uuid, p_key_hash text)
RETURNS TABLE(route_prefix text, status text)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT route_prefix, status FROM deployments WHERE id = p_id AND predict_key_hash = p_key_hash;
$$;
REVOKE ALL ON FUNCTION deployment_for_predict(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION deployment_for_predict(uuid, text) TO core_app;
