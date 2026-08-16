-- Background reconciliation needs to find in-flight runs across all
-- tenants without an app.tenant_id already set (see app/training.py's
-- reconcile_inflight_runs) — same SECURITY DEFINER bypass pattern as
-- project_exists_any_tenant in 0001_tenancy.sql.
CREATE OR REPLACE FUNCTION inflight_training_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT DISTINCT tenant_id FROM training_runs WHERE status IN ('submitted', 'running');
$$;
REVOKE ALL ON FUNCTION inflight_training_tenant_ids() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION inflight_training_tenant_ids() TO core_app;
