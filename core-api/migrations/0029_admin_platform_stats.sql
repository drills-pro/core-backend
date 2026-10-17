-- migrate:up
-- Platform-wide counters for the back-office stat cards (app/modules/admin),
-- the strip that sits above the tenant list.
--
-- Same SECURITY DEFINER escape as admin_tenant_stats (0028): projects and
-- project_members are RLS-protected and the admin routes run on a
-- connection with no `app.tenant_id`, so a plain COUNT there returns 0 for
-- everyone. This one takes no arguments because it is deliberately
-- platform-wide, and it still hands back aggregates only, never rows.
--
-- Status (active/suspended) is not in here: Zitadel owns it, so the route
-- counts that side itself.
CREATE OR REPLACE FUNCTION admin_platform_stats()
RETURNS TABLE(
  tenant_count bigint,
  tenant_count_30d bigint,
  project_count bigint,
  project_count_30d bigint,
  member_count bigint
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT (SELECT count(*) FROM tenants),
         (SELECT count(*) FROM tenants WHERE created_at >= now() - interval '30 days'),
         -- Soft-deleted projects are excluded, as they are in the listing's
         -- per-tenant counter (0028), so the card and the column agree.
         (SELECT count(*) FROM projects WHERE deleted_at IS NULL),
         (SELECT count(*) FROM projects
           WHERE deleted_at IS NULL AND created_at >= now() - interval '30 days'),
         -- Distinct *people*, not memberships: someone on three projects in
         -- two tenants is one member of the platform. This is why it can be
         -- smaller than the sum of the listing's per-tenant member counts,
         -- which count the same person once per tenant.
         (SELECT count(DISTINCT user_id) FROM project_members);
$$;
REVOKE ALL ON FUNCTION admin_platform_stats() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_platform_stats() TO core_app;

-- migrate:down
DROP FUNCTION IF EXISTS admin_platform_stats();
