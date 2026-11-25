-- migrate:up
-- Models without stages (KISS): a deploy is the only step. Promotion to Production, its worker and its columns go.
-- Callers that name no model reach the project's `main` deployment: at most one per project, kept by this index.
-- The `stage` column stays, always 'None' for new rows, until the admin model listings stop returning it.
DROP INDEX model_versions_one_pending_per_model;
DROP FUNCTION pending_promotion_tenant_ids();
-- migration-lint: allow-destructive (the promotion columns and the stage flow are removed on purpose, with no compatibility layer)
ALTER TABLE model_versions
  DROP COLUMN promotion_status, DROP COLUMN promotion_error, DROP COLUMN promotion_error_code, DROP COLUMN promotion_actor,
  DROP COLUMN promotion_acknowledged_risk, DROP COLUMN promotion_requested_at;

ALTER TABLE deployments ADD COLUMN main boolean NOT NULL DEFAULT false;
CREATE UNIQUE INDEX deployments_one_main_per_project ON deployments(project_id) WHERE main AND status <> 'deleted';

-- migrate:down
DROP INDEX deployments_one_main_per_project;
ALTER TABLE deployments DROP COLUMN main;
ALTER TABLE model_versions
  ADD COLUMN promotion_status text CONSTRAINT model_versions_promotion_status_check CHECK (promotion_status IN ('pending', 'failed')),
  ADD COLUMN promotion_error text,
  ADD COLUMN promotion_error_code text,
  ADD COLUMN promotion_actor text,
  ADD COLUMN promotion_acknowledged_risk boolean NOT NULL DEFAULT false,
  ADD COLUMN promotion_requested_at timestamptz;
CREATE UNIQUE INDEX model_versions_one_pending_per_model ON model_versions(model_id) WHERE promotion_status = 'pending';
CREATE FUNCTION pending_promotion_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT DISTINCT v.tenant_id FROM model_versions v JOIN tenants t ON t.id = v.tenant_id AND t.status = 'active'
  WHERE v.promotion_status = 'pending';
$$;
REVOKE ALL ON FUNCTION pending_promotion_tenant_ids() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION pending_promotion_tenant_ids() TO core_app;
