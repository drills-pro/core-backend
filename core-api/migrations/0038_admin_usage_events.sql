-- Cross-tenant read of the usage ledger, for the back-office activity log
-- (app/modules/admin/usage).
--
-- usage_events is RLS-protected (0003) and the admin routes run on a
-- connection that sets no `app.tenant_id`, so a plain SELECT there returns
-- nothing at all. Same SECURITY DEFINER escape as admin_activity (0029),
-- and the same audience: the grant is to core_app only, and every route
-- calling it sits behind get_platform_admin.

-- The listing orders by time across every tenant, which
-- usage_events_tenant_idx (0003) cannot serve — it leads with tenant_id.
-- id breaks ties so rows that share an occurred_at keep a stable order
-- across pages.
CREATE INDEX IF NOT EXISTS usage_events_occurred_at_idx ON usage_events(occurred_at DESC, id DESC);

CREATE OR REPLACE FUNCTION admin_usage_events(
  p_limit int,
  p_offset int,
  p_tenant_id uuid,
  p_resource_type text,
  p_q text,
  p_since timestamptz
)
RETURNS TABLE(
  id bigint,
  tenant_id uuid,
  tenant_name text,
  project_id uuid,
  project_name text,
  resource_type text,
  quantity numeric,
  unit text,
  occurred_at timestamptz,
  source_ref text,
  pricing_version text,
  -- The size of the whole filtered set, repeated on every row, so a pager
  -- costs no second round trip. Same job as admin_activity's.
  total bigint
)
LANGUAGE sql SECURITY DEFINER AS $$
  WITH matching AS (
    SELECT u.id, u.tenant_id, t.name AS tenant_name,
           u.project_id, p.name AS project_name,
           u.resource_type, u.quantity, u.unit, u.occurred_at,
           u.source_ref, u.pricing_version
    FROM usage_events u
    JOIN tenants t ON t.id = u.tenant_id
    -- LEFT even though project_id is NOT NULL: a soft-deleted project keeps
    -- its row, but the ledger should never depend on that staying true.
    LEFT JOIN projects p ON p.id = u.project_id
    WHERE (p_tenant_id IS NULL OR u.tenant_id = p_tenant_id)
      AND (p_resource_type IS NULL OR u.resource_type = p_resource_type)
      AND (p_since IS NULL OR u.occurred_at >= p_since)
      AND (p_q IS NULL
           OR u.resource_type ILIKE '%' || p_q || '%'
           OR u.source_ref ILIKE '%' || p_q || '%'
           OR t.name ILIKE '%' || p_q || '%'
           OR p.name ILIKE '%' || p_q || '%')
  )
  SELECT m.*, count(*) OVER ()
  FROM matching m
  ORDER BY m.occurred_at DESC, m.id DESC
  LIMIT p_limit OFFSET p_offset;
$$;
REVOKE ALL ON FUNCTION admin_usage_events(int, int, uuid, text, text, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_usage_events(int, int, uuid, text, text, timestamptz) TO core_app;
