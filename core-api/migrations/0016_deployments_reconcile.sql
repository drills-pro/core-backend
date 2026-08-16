-- Same SECURITY DEFINER bypass pattern as inflight_training_tenant_ids
-- (0011) / inflight_import_tenant_ids (0013) — background reconciliation
-- needs in-flight deployments across all tenants without an
-- app.tenant_id already set (see app/deployments.py's
-- reconcile_inflight_deployments).
CREATE OR REPLACE FUNCTION inflight_deploy_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT DISTINCT tenant_id FROM deployments WHERE status = 'deploying';
$$;
REVOKE ALL ON FUNCTION inflight_deploy_tenant_ids() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION inflight_deploy_tenant_ids() TO core_app;
