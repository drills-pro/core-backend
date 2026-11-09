-- migrate:up
-- A suspended tenant (or one pending deletion) and an archived or deleted project must not predict
-- (planning/24, audit A-04). Both key lookups now return the tenant's status so the app answers
-- 503 tenant_suspended before Serve is called, and leave out rows of a project that is not active,
-- which answer like an unknown key (the same 401). RETURNS TABLE changes, so drop and recreate.
DROP FUNCTION deployment_for_predict(uuid, text);
CREATE FUNCTION deployment_for_predict(p_id uuid, p_key_hash text)
RETURNS TABLE(route_prefix text, status text, rate_limit_per_minute integer, tenant_id uuid, project_id uuid, tenant_status text)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT d.route_prefix, d.status, d.rate_limit_per_minute, d.tenant_id, d.project_id, t.status
  FROM deployments d
  JOIN tenants t ON t.id = d.tenant_id
  JOIN projects p ON p.id = d.project_id AND p.status = 'active'
  WHERE d.id = p_id AND d.predict_key_hash = p_key_hash;
$$;
REVOKE ALL ON FUNCTION deployment_for_predict(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION deployment_for_predict(uuid, text) TO core_app;

DROP FUNCTION api_key_lookup(text);
CREATE FUNCTION api_key_lookup(p_key_hash text)
RETURNS TABLE(key_id uuid, tenant_id uuid, project_id uuid, scopes text[], model_ids uuid[], tenant_status text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT k.id, k.tenant_id, k.project_id,
         COALESCE((SELECT array_agg(s.scope) FROM api_key_scopes s WHERE s.key_id = k.id), '{}'),
         COALESCE((SELECT array_agg(m.model_id) FROM api_key_models m WHERE m.key_id = k.id), '{}'),
         t.status
  FROM api_keys k
  JOIN tenants t ON t.id = k.tenant_id
  JOIN projects p ON p.id = k.project_id AND p.status = 'active'
  WHERE k.key_hash = p_key_hash AND k.revoked_at IS NULL AND (k.expires_at IS NULL OR k.expires_at > now());
$$;
REVOKE ALL ON FUNCTION api_key_lookup(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION api_key_lookup(text) TO core_app;

-- migrate:down
DROP FUNCTION api_key_lookup(text);
CREATE FUNCTION api_key_lookup(p_key_hash text)
RETURNS TABLE(key_id uuid, tenant_id uuid, project_id uuid, scopes text[], model_ids uuid[])
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT k.id, k.tenant_id, k.project_id,
         COALESCE((SELECT array_agg(s.scope) FROM api_key_scopes s WHERE s.key_id = k.id), '{}'),
         COALESCE((SELECT array_agg(m.model_id) FROM api_key_models m WHERE m.key_id = k.id), '{}')
  FROM api_keys k
  WHERE k.key_hash = p_key_hash AND k.revoked_at IS NULL AND (k.expires_at IS NULL OR k.expires_at > now());
$$;
REVOKE ALL ON FUNCTION api_key_lookup(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION api_key_lookup(text) TO core_app;

DROP FUNCTION deployment_for_predict(uuid, text);
CREATE FUNCTION deployment_for_predict(p_id uuid, p_key_hash text)
RETURNS TABLE(route_prefix text, status text, rate_limit_per_minute integer, tenant_id uuid, project_id uuid)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT route_prefix, status, rate_limit_per_minute, tenant_id, project_id FROM deployments
  WHERE id = p_id AND predict_key_hash = p_key_hash;
$$;
REVOKE ALL ON FUNCTION deployment_for_predict(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION deployment_for_predict(uuid, text) TO core_app;
