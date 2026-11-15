-- migrate:up
-- Who changed platform data through /v1/admin, append-only. Platform-level like job_package_audit: there is no tenant, so
-- no RLS, and audit_log (tenant-scoped) cannot hold it. core_app may only add and read rows, never change or remove one.
CREATE TABLE admin_audit (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  actor text NOT NULL,
  action text NOT NULL CONSTRAINT admin_audit_action_check CHECK (action IN (
    'tenant.create', 'tenant.set_status', 'tenant.update_settings', 'task_type.put', 'task_preset.put', 'task_preset.delete',
    'model_catalog.create', 'model_catalog.update', 'model_catalog.enable', 'model_catalog.disable', 'model_catalog.delete',
    'plan_limits.put')),
  entity_type text NOT NULL CONSTRAINT admin_audit_entity_type_check CHECK (entity_type IN (
    'tenant', 'task_type', 'task_preset', 'model_catalog', 'plan_limits')),
  entity_id text NOT NULL,
  detail jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX admin_audit_created_at_idx ON admin_audit (created_at DESC, id DESC);
GRANT SELECT, INSERT ON admin_audit TO core_app;

-- migrate:down
DROP TABLE admin_audit;
