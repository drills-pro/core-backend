-- migrate:up
-- The cross-tenant sweeps skip a tenant that is not active (audit A-04): a suspended or pending_delete tenant's
-- in-flight rows wait, untouched, until it is active again. Same signatures, so the grants stay.
CREATE OR REPLACE FUNCTION inflight_training_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT DISTINCT r.tenant_id FROM training_runs r JOIN tenants t ON t.id = r.tenant_id AND t.status = 'active'
  WHERE r.status IN ('submitted', 'running');
$$;

CREATE OR REPLACE FUNCTION inflight_import_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT DISTINCT i.tenant_id FROM dataset_imports i JOIN tenants t ON t.id = i.tenant_id AND t.status = 'active'
  WHERE i.status IN ('queued', 'running', 'committing');
$$;

CREATE OR REPLACE FUNCTION inflight_deploy_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT DISTINCT d.tenant_id FROM deployments d JOIN tenants t ON t.id = d.tenant_id AND t.status = 'active'
  WHERE d.status = 'deploying';
$$;

CREATE OR REPLACE FUNCTION inflight_prediction_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT DISTINCT p.tenant_id FROM predictions p JOIN tenants t ON t.id = p.tenant_id AND t.status = 'active'
  WHERE p.mode = 'background' AND p.status IN ('queued', 'in_progress');
$$;

-- migrate:down
CREATE OR REPLACE FUNCTION inflight_training_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT DISTINCT tenant_id FROM training_runs WHERE status IN ('submitted', 'running');
$$;

CREATE OR REPLACE FUNCTION inflight_import_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT DISTINCT tenant_id FROM dataset_imports WHERE status IN ('queued', 'running', 'committing');
$$;

CREATE OR REPLACE FUNCTION inflight_deploy_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT DISTINCT tenant_id FROM deployments WHERE status = 'deploying';
$$;

CREATE OR REPLACE FUNCTION inflight_prediction_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT DISTINCT tenant_id FROM predictions WHERE mode = 'background' AND status IN ('queued', 'in_progress');
$$;
