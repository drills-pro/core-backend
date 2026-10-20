-- migrate:up
-- audit_log ids become UUIDv7 (time-ordered, native in Postgres 18): no sequence to grant or leak a row count,
-- ids are safe to expose in the tenant-facing audit API, and they stay index-friendly. Existing rows get a
-- UUIDv7 stamped with their own created_at, so id order still follows time.
DROP FUNCTION admin_activity(int, int, uuid, text, text, timestamptz);
ALTER TABLE audit_log ADD COLUMN new_id uuid;
UPDATE audit_log SET new_id = uuidv7(created_at - now());
-- migration-lint: allow-destructive (id column type swap, every row is rewritten with a derived uuid)
ALTER TABLE audit_log DROP CONSTRAINT audit_log_pkey, DROP COLUMN id;
ALTER TABLE audit_log RENAME COLUMN new_id TO id;
ALTER TABLE audit_log ALTER COLUMN id SET DEFAULT uuidv7(), ALTER COLUMN id SET NOT NULL, ADD PRIMARY KEY (id);
-- Keyset paging orders by (created_at DESC, id DESC), so the indexes end in both columns; the tenant-wide feed
-- needs tenant_id first or it walks the global index discarding other tenants' rows. Dropping the id column took
-- the global index with it; the project one is replaced.
DROP INDEX audit_log_tenant_project_idx;
CREATE INDEX audit_log_tenant_project_idx ON audit_log(tenant_id, project_id, created_at DESC, id DESC);
CREATE INDEX audit_log_tenant_created_idx ON audit_log(tenant_id, created_at DESC, id DESC);
CREATE INDEX audit_log_created_at_idx ON audit_log(created_at DESC, id DESC);

CREATE FUNCTION admin_activity(
  p_limit int,
  p_offset int,
  p_tenant_id uuid,
  p_entity_type text,
  p_q text,
  p_since timestamptz
)
RETURNS TABLE(
  id uuid,
  tenant_id uuid,
  tenant_name text,
  project_id uuid,
  project_name text,
  actor text,
  action text,
  entity_type text,
  entity_id text,
  created_at timestamptz,
  total bigint
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH matching AS (
    SELECT a.id, a.tenant_id, t.name AS tenant_name,
           a.project_id, p.name AS project_name,
           a.actor, a.action, a.entity_type, a.entity_id, a.created_at
    FROM audit_log a
    JOIN tenants t ON t.id = a.tenant_id
    -- LEFT: a project that went away should not take its history with it.
    LEFT JOIN projects p ON p.id = a.project_id
    WHERE (p_tenant_id IS NULL OR a.tenant_id = p_tenant_id)
      AND (p_entity_type IS NULL OR a.entity_type = p_entity_type)
      AND (p_since IS NULL OR a.created_at >= p_since)
      AND (p_q IS NULL
           OR a.actor ILIKE '%' || p_q || '%'
           OR a.action ILIKE '%' || p_q || '%'
           OR a.entity_type ILIKE '%' || p_q || '%'
           OR a.entity_id ILIKE '%' || p_q || '%'
           OR t.name ILIKE '%' || p_q || '%'
           OR p.name ILIKE '%' || p_q || '%')
  )
  SELECT m.*, count(*) OVER ()
  FROM matching m
  ORDER BY m.created_at DESC, m.id DESC
  LIMIT p_limit OFFSET p_offset;
$$;
REVOKE ALL ON FUNCTION admin_activity(int, int, uuid, text, text, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_activity(int, int, uuid, text, text, timestamptz) TO core_app;

-- migrate:down
DROP FUNCTION admin_activity(int, int, uuid, text, text, timestamptz);
ALTER TABLE audit_log ADD COLUMN old_id bigserial;
ALTER TABLE audit_log DROP CONSTRAINT audit_log_pkey, DROP COLUMN id;
ALTER TABLE audit_log RENAME COLUMN old_id TO id;
ALTER TABLE audit_log ADD PRIMARY KEY (id);
-- Dropping the id column took every index that ended in it; restore the pre-UUID definitions.
CREATE INDEX audit_log_tenant_project_idx ON audit_log(tenant_id, project_id, created_at DESC);
CREATE INDEX audit_log_created_at_idx ON audit_log(created_at DESC, id DESC);
ALTER SEQUENCE audit_log_old_id_seq RENAME TO audit_log_id_seq;
GRANT USAGE ON SEQUENCE audit_log_id_seq TO core_app;

CREATE FUNCTION admin_activity(
  p_limit int,
  p_offset int,
  p_tenant_id uuid,
  p_entity_type text,
  p_q text,
  p_since timestamptz
)
RETURNS TABLE(
  id bigint,
  tenant_id uuid,
  tenant_name text,
  project_id uuid,
  project_name text,
  actor text,
  action text,
  entity_type text,
  entity_id text,
  created_at timestamptz,
  total bigint
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH matching AS (
    SELECT a.id, a.tenant_id, t.name AS tenant_name,
           a.project_id, p.name AS project_name,
           a.actor, a.action, a.entity_type, a.entity_id, a.created_at
    FROM audit_log a
    JOIN tenants t ON t.id = a.tenant_id
    -- LEFT: a project that went away should not take its history with it.
    LEFT JOIN projects p ON p.id = a.project_id
    WHERE (p_tenant_id IS NULL OR a.tenant_id = p_tenant_id)
      AND (p_entity_type IS NULL OR a.entity_type = p_entity_type)
      AND (p_since IS NULL OR a.created_at >= p_since)
      AND (p_q IS NULL
           OR a.actor ILIKE '%' || p_q || '%'
           OR a.action ILIKE '%' || p_q || '%'
           OR a.entity_type ILIKE '%' || p_q || '%'
           OR a.entity_id ILIKE '%' || p_q || '%'
           OR t.name ILIKE '%' || p_q || '%'
           OR p.name ILIKE '%' || p_q || '%')
  )
  SELECT m.*, count(*) OVER ()
  FROM matching m
  ORDER BY m.created_at DESC, m.id DESC
  LIMIT p_limit OFFSET p_offset;
$$;
REVOKE ALL ON FUNCTION admin_activity(int, int, uuid, text, text, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_activity(int, int, uuid, text, text, timestamptz) TO core_app;
