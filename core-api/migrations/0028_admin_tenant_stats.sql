-- migrate:up
-- Back-office counters for the /v1/admin tenant list (app/modules/admin).
--
-- projects and project_members are RLS-protected and the admin routes run
-- on a connection with no `app.tenant_id` set, so a plain COUNT from there
-- returns 0 for every tenant. Same SECURITY DEFINER escape as
-- project_exists_any_tenant (0001) and list_public_* (0024): scoped to
-- aggregate counts for the tenant ids the caller already holds, so it
-- hands back numbers, never another tenant's rows.
CREATE OR REPLACE FUNCTION admin_tenant_stats(p_tenant_ids uuid[])
RETURNS TABLE(tenant_id uuid, project_count bigint, member_count bigint)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT t.id,
         (SELECT count(*) FROM projects p
           WHERE p.tenant_id = t.id AND p.deleted_at IS NULL),
         -- project_members is the only membership record this schema keeps
         -- (0002: "no local users table, Zitadel is the source of truth"),
         -- so a tenant's member count is its distinct project members. An
         -- org member who is on no project yet isn't counted here.
         (SELECT count(DISTINCT m.user_id) FROM project_members m
           WHERE m.tenant_id = t.id)
  FROM unnest(p_tenant_ids) AS t(id);
$$;
REVOKE ALL ON FUNCTION admin_tenant_stats(uuid[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_tenant_stats(uuid[]) TO core_app;

-- migrate:down
DROP FUNCTION IF EXISTS admin_tenant_stats(uuid[]);
