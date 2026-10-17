-- migrate:up
-- /predict (app/modules/user/deployments/routes.py) reads rate_limit_per_minute from
-- deployment_for_predict, but 0026 widened the function for usage metering and left that column
-- out, so every call to an active deployment failed with a KeyError. Return both: the rate limit
-- (0013) and the tenant/project ids (0026).
DROP FUNCTION IF EXISTS deployment_for_predict(uuid, text);
CREATE FUNCTION deployment_for_predict(p_id uuid, p_key_hash text)
RETURNS TABLE(route_prefix text, status text, rate_limit_per_minute integer, tenant_id uuid, project_id uuid)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT route_prefix, status, rate_limit_per_minute, tenant_id, project_id FROM deployments
  WHERE id = p_id AND predict_key_hash = p_key_hash;
$$;
REVOKE ALL ON FUNCTION deployment_for_predict(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION deployment_for_predict(uuid, text) TO core_app;

-- migrate:down
-- Back to 0026's shape.
DROP FUNCTION IF EXISTS deployment_for_predict(uuid, text);
CREATE FUNCTION deployment_for_predict(p_id uuid, p_key_hash text)
RETURNS TABLE(route_prefix text, status text, tenant_id uuid, project_id uuid)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT route_prefix, status, tenant_id, project_id FROM deployments WHERE id = p_id AND predict_key_hash = p_key_hash;
$$;
REVOKE ALL ON FUNCTION deployment_for_predict(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION deployment_for_predict(uuid, text) TO core_app;
