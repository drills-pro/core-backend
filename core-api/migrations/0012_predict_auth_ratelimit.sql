-- Extend deployment_for_predict to also return rate_limit_per_minute so
-- app/deployments.py::predict can enforce it without a second query.
CREATE OR REPLACE FUNCTION deployment_for_predict(p_id uuid, p_key_hash text)
RETURNS TABLE(route_prefix text, status text, rate_limit_per_minute integer)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT route_prefix, status, rate_limit_per_minute FROM deployments
  WHERE id = p_id AND predict_key_hash = p_key_hash;
$$;
REVOKE ALL ON FUNCTION deployment_for_predict(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION deployment_for_predict(uuid, text) TO core_app;
