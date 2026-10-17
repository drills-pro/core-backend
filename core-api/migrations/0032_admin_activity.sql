-- migrate:up
-- Cross-tenant read of the audit trail, for the back-office activity log
-- (app/modules/admin/activity).
--
-- audit_log is RLS-protected (0002) and the admin routes run on a
-- connection that sets no `app.tenant_id`, so a plain SELECT there returns
-- nothing at all — not a truncated list, an empty one. Same SECURITY
-- DEFINER escape as admin_tenant_stats (0028) and list_public_projects
-- (0024).
--
-- Unlike the admin counters, this one does hand back rows: rows are what an
-- audit trail is, and the operator's back-office is the one audience
-- req/01 FR1 allows to see tenants side by side. The grant is to core_app
-- only, and every route calling it sits behind get_platform_admin.

-- The listing orders by time across every tenant, which
-- audit_log_tenant_project_idx (0002) cannot serve — it leads with
-- tenant_id. id breaks ties so rows written in the same transaction, which
-- share a created_at, keep a stable order across pages.
CREATE INDEX IF NOT EXISTS audit_log_created_at_idx ON audit_log(created_at DESC, id DESC);

CREATE OR REPLACE FUNCTION admin_activity(
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
  -- The size of the whole filtered set, repeated on every row, so a pager
  -- costs no second round trip. Same job as the tenant listing's
  -- X-Total-Count header.
  total bigint
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH matching AS (
    SELECT a.id, a.tenant_id, t.name AS tenant_name,
           a.project_id, p.name AS project_name,
           a.actor, a.action, a.entity_type, a.entity_id, a.created_at
    FROM audit_log a
    JOIN tenants t ON t.id = a.tenant_id
    -- LEFT, twice over: an action on the tenant itself has no project, and
    -- a project row that went away should not take its history with it.
    -- Soft-deleted projects are kept for the same reason — the log says
    -- what happened, not what still exists.
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

-- The totals behind the activity log's stat strip. Platform-wide by
-- definition, so it takes no arguments, and aggregates only.
--
-- Dropped rather than replaced: the column list has changed since this
-- migration was first applied, and CREATE OR REPLACE cannot widen or
-- narrow a function's return type.
DROP FUNCTION IF EXISTS admin_activity_stats();
--
-- "Today" is the database's day, in whatever timezone the server keeps —
-- the same clock that stamped created_at, so the count and the rows under
-- the "Today" heading always agree with each other.
CREATE OR REPLACE FUNCTION admin_activity_stats()
RETURNS TABLE(
  events_today bigint,
  -- How many organisations those events touched, which is the line under
  -- the count: one busy tenant and ten quiet ones read very differently.
  tenants_today bigint
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT count(*) FILTER (WHERE created_at >= date_trunc('day', now())),
         count(DISTINCT tenant_id) FILTER (WHERE created_at >= date_trunc('day', now()))
  FROM audit_log;
$$;
REVOKE ALL ON FUNCTION admin_activity_stats() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_activity_stats() TO core_app;

-- migrate:down
DROP FUNCTION IF EXISTS admin_activity_stats();
DROP FUNCTION IF EXISTS admin_activity(int, int, uuid, text, text, timestamptz);
DROP INDEX IF EXISTS audit_log_created_at_idx;
