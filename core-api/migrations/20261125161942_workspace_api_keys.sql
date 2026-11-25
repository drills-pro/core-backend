-- migrate:up
-- Workspace API keys: a key belongs to the workspace (tenant_id) and works in the projects granted to it (api_key_projects).
-- A key made before this was bound to one project; that project becomes its only grant.

CREATE TABLE api_key_projects (
  key_id     uuid NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
  tenant_id  uuid NOT NULL REFERENCES tenants(id),
  project_id uuid NOT NULL REFERENCES projects(id),
  PRIMARY KEY (key_id, project_id)
);
CREATE INDEX api_key_projects_project_id_idx ON api_key_projects(project_id);

ALTER TABLE api_key_projects ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON api_key_projects USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT ON api_key_projects TO core_app;

INSERT INTO api_key_projects (key_id, tenant_id, project_id) SELECT id, tenant_id, project_id FROM api_keys;

-- migration-lint: allow-destructive (the grant rows above carry the project, so a key is no longer bound to one)
ALTER TABLE api_keys DROP COLUMN project_id;

DROP FUNCTION api_key_lookup(text);
CREATE FUNCTION api_key_lookup(p_key_hash text)
RETURNS TABLE(key_id uuid, tenant_id uuid, project_ids uuid[], scopes text[], model_ids uuid[], tenant_status text, rate_limit_per_minute integer)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT k.id, k.tenant_id,
         COALESCE((SELECT array_agg(p.id) FROM api_key_projects kp JOIN projects p ON p.id = kp.project_id AND p.status = 'active'
                   WHERE kp.key_id = k.id), '{}'),
         COALESCE((SELECT array_agg(s.scope) FROM api_key_scopes s WHERE s.key_id = k.id), '{}'),
         COALESCE((SELECT array_agg(m.model_id) FROM api_key_models m WHERE m.key_id = k.id), '{}'),
         t.status, k.rate_limit_per_minute
  FROM api_keys k
  JOIN tenants t ON t.id = k.tenant_id
  WHERE k.key_hash = p_key_hash AND k.revoked_at IS NULL AND (k.expires_at IS NULL OR k.expires_at > now());
$$;

REVOKE ALL ON FUNCTION api_key_lookup(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION api_key_lookup(text) TO core_app;

-- migrate:down
DROP FUNCTION api_key_lookup(text);
ALTER TABLE api_keys ADD COLUMN project_id uuid REFERENCES projects(id);
UPDATE api_keys k SET project_id = (SELECT kp.project_id FROM api_key_projects kp WHERE kp.key_id = k.id ORDER BY kp.project_id LIMIT 1);
ALTER TABLE api_keys ALTER COLUMN project_id SET NOT NULL;
CREATE INDEX api_keys_project_id_idx ON api_keys(project_id);
DROP TABLE api_key_projects;

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
