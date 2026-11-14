-- migrate:up
-- Legacy serving (a Serve app per deployment) is gone: core-api declares the shared predictor apps without asking first,
-- and a leftover serving = 'legacy' row is answered by them like any other. The column and its rows stay as they are.
DROP FUNCTION legacy_deployments_active();

-- migrate:down
CREATE FUNCTION legacy_deployments_active() RETURNS bigint
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT count(*) FROM deployments WHERE serving = 'legacy' AND status IN ('deploying', 'active');
$$;
REVOKE ALL ON FUNCTION legacy_deployments_active() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION legacy_deployments_active() TO core_app;
