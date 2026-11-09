-- migrate:up
-- Optional per-key limit for POST /v1/predict (planning/24, Rate limits): the lower of this and the plan limit applies.
-- NULL = no key limit. api_key_lookup returns it, so require_key has it without a second query; RETURNS TABLE changes,
-- so drop and recreate.
ALTER TABLE api_keys ADD COLUMN rate_limit_per_minute integer CHECK (rate_limit_per_minute > 0);

DROP FUNCTION api_key_lookup(text);
CREATE FUNCTION api_key_lookup(p_key_hash text)
RETURNS TABLE(key_id uuid, tenant_id uuid, project_id uuid, scopes text[], model_ids uuid[], tenant_status text, rate_limit_per_minute integer)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT k.id, k.tenant_id, k.project_id,
         COALESCE((SELECT array_agg(s.scope) FROM api_key_scopes s WHERE s.key_id = k.id), '{}'),
         COALESCE((SELECT array_agg(m.model_id) FROM api_key_models m WHERE m.key_id = k.id), '{}'),
         t.status, k.rate_limit_per_minute
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

ALTER TABLE api_keys DROP COLUMN rate_limit_per_minute;
