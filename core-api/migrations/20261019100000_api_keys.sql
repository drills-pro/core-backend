-- migrate:up
-- Project-scoped API keys (planning/04, 05): the key authenticates a caller and carries its own scopes
-- and optional model allowlist. Only the sha256 of the secret is stored. Scopes and models are rows.

CREATE TABLE api_keys (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id),
  project_id  uuid NOT NULL REFERENCES projects(id),
  name        text NOT NULL,
  prefix      text NOT NULL,  -- short and non-secret, for display
  key_hash    text NOT NULL UNIQUE,
  created_by  text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz,
  revoked_at  timestamptz
);
CREATE INDEX api_keys_project_id_idx ON api_keys(project_id);

CREATE TABLE api_key_scopes (
  key_id     uuid NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
  tenant_id  uuid NOT NULL REFERENCES tenants(id),
  scope      text NOT NULL,
  PRIMARY KEY (key_id, scope),
  CONSTRAINT api_key_scopes_scope_check CHECK (scope IN ('predict'))
);

-- No rows for a key = every model in the project. model_id has no foreign key: the model entity
-- predict addresses is not settled yet.
CREATE TABLE api_key_models (
  key_id     uuid NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
  tenant_id  uuid NOT NULL REFERENCES tenants(id),
  model_id   uuid NOT NULL,
  PRIMARY KEY (key_id, model_id)
);

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['api_keys', 'api_key_scopes', 'api_key_models']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_setting(''app.tenant_id'', true)::uuid)', t);
  END LOOP;
END
$$;

GRANT SELECT, INSERT, UPDATE ON api_keys TO core_app;
GRANT SELECT, INSERT ON api_key_scopes, api_key_models TO core_app;

-- A key arrives before any tenant context exists, so the lookup runs as the owner. Revoked and expired
-- keys return no row, which is why revocation takes effect on the next request.
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

-- migrate:down
DROP FUNCTION api_key_lookup(text);
DROP TABLE api_key_models, api_key_scopes, api_keys;
