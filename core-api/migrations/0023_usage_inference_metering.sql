-- Usage — org/team/project two-tier rollup (improvement/phase2/09-usage-billing-two-tier.md).
-- FR2: /predict needs tenant_id/project_id to record inference usage, but
-- the existing deployment_for_predict() (0009) only returns route/status —
-- widen it (return-type change needs DROP first in Postgres).
DROP FUNCTION IF EXISTS deployment_for_predict(uuid, text);
CREATE FUNCTION deployment_for_predict(p_id uuid, p_key_hash text)
RETURNS TABLE(route_prefix text, status text, tenant_id uuid, project_id uuid)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT route_prefix, status, tenant_id, project_id FROM deployments WHERE id = p_id AND predict_key_hash = p_key_hash;
$$;
REVOKE ALL ON FUNCTION deployment_for_predict(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION deployment_for_predict(uuid, text) TO core_app;
