-- migrate:up
-- Declaring the shared predictor apps (PUT /api/serve/applications/) replaces every Serve app that is not in the config,
-- and that includes the per-deployment apps of legacy deployments. Before it does that, core-api asks here whether any is
-- still being served. Across tenants, so SECURITY DEFINER like the other cross-tenant reads; it returns a count, no rows.
CREATE FUNCTION legacy_deployments_active() RETURNS bigint
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT count(*) FROM deployments WHERE serving = 'legacy' AND status IN ('deploying', 'active');
$$;
REVOKE ALL ON FUNCTION legacy_deployments_active() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION legacy_deployments_active() TO core_app;

-- migrate:down
DROP FUNCTION legacy_deployments_active();
