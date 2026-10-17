-- migrate:up
-- Which projects a member is on, not just how many.
--
-- admin_tenant_members (0030) already folds a person's memberships into
-- one row and aggregates the roles they hold; the back-office members
-- table now has a Projects column, and a count alone does not answer what
-- an operator opens it to ask. The rows are already grouped by user, so
-- this is the same aggregate the roles take, over the project names the
-- join already has in hand — no extra scan.
--
-- Dropped rather than replaced: the return type gains a column, which
-- CREATE OR REPLACE cannot do.
DROP FUNCTION IF EXISTS admin_tenant_members(uuid);

CREATE OR REPLACE FUNCTION admin_tenant_members(p_tenant_id uuid)
RETURNS TABLE(
  user_id text,
  roles text[],
  -- The names, alphabetical (array_agg DISTINCT sorts), so two reads of
  -- the same row list them in the same order.
  projects text[],
  project_count bigint,
  joined_at timestamptz
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT m.user_id,
         array_agg(DISTINCT m.role),
         array_agg(DISTINCT p.name),
         count(DISTINCT m.project_id),
         min(m.created_at)
  FROM project_members m
  -- Soft-deleted projects stay excluded, as in 0030: a membership of a
  -- project that is on its way out is not access anyone has.
  JOIN projects p ON p.id = m.project_id AND p.deleted_at IS NULL
  WHERE m.tenant_id = p_tenant_id
  GROUP BY m.user_id
  ORDER BY min(m.created_at);
$$;
REVOKE ALL ON FUNCTION admin_tenant_members(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_tenant_members(uuid) TO core_app;

-- migrate:down
-- Back to 0030's shape.
DROP FUNCTION IF EXISTS admin_tenant_members(uuid);
CREATE FUNCTION admin_tenant_members(p_tenant_id uuid)
RETURNS TABLE(user_id text, roles text[], project_count bigint, joined_at timestamptz)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT m.user_id,
         array_agg(DISTINCT m.role),
         count(DISTINCT m.project_id),
         min(m.created_at)
  FROM project_members m
  JOIN projects p ON p.id = m.project_id AND p.deleted_at IS NULL
  WHERE m.tenant_id = p_tenant_id
  GROUP BY m.user_id
  ORDER BY min(m.created_at);
$$;
REVOKE ALL ON FUNCTION admin_tenant_members(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_tenant_members(uuid) TO core_app;
