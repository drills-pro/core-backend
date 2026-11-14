-- migrate:up
-- Task bundles (planning/25, ADR-030): job_packages also holds one zip per task, kind 'task', named task_<slug>, with the
-- manifest.json it was registered with. Runtime packages (kind 'runtime', manifest null) keep their closed list of names;
-- ray_training now holds ray_core, the contract bundles run on. Existing rows are runtime packages, so the defaults keep them
-- valid and the old INSERT working.
-- Platform data like job_packages: no tenant_id, so no RLS, and only /v1/admin routes (and the auto-promote poller) write it.
ALTER TABLE job_packages
  ADD COLUMN kind text NOT NULL DEFAULT 'runtime' CONSTRAINT job_packages_kind_check CHECK (kind IN ('runtime', 'task')),
  ADD COLUMN manifest jsonb;
ALTER TABLE job_packages DROP CONSTRAINT job_packages_name_check;
ALTER TABLE job_packages ADD CONSTRAINT job_packages_name_check CHECK (
  (kind = 'runtime' AND name IN ('ray_jobs', 'ray_training', 'ray_serving'))
  OR (kind = 'task' AND name ~ '^task_[a-z][a-z0-9-]*$'));
ALTER TABLE job_packages ADD CONSTRAINT job_packages_manifest_check CHECK ((kind = 'task') = (manifest IS NOT NULL));

-- The register and status changes of a bundle, append-only: there is no tenant, so audit_log (tenant-scoped) cannot hold them.
CREATE TABLE job_package_audit (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  name text NOT NULL,
  sha256 text NOT NULL,
  action text NOT NULL CONSTRAINT job_package_audit_action_check CHECK (action IN ('register', 'promote', 'retire')),
  actor text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (name, sha256) REFERENCES job_packages (name, sha256)
);
GRANT SELECT, INSERT ON job_package_audit TO core_app;

-- Per task slug: whether a bundle whose smoke passed is promoted without a person. No row = the default (true).
CREATE TABLE task_bundle_settings (
  slug text PRIMARY KEY,
  auto_promote boolean NOT NULL DEFAULT true,
  updated_by text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT, UPDATE ON task_bundle_settings TO core_app;

-- task_types rows written by a promotion name the bundle they came from; null = a row from before bundles.
ALTER TABLE task_types ADD COLUMN bundle_sha256 text CHECK (bundle_sha256 ~ '^[0-9a-f]{64}$');
-- A preset can be switched off without deleting it (a promotion rewrites the values, not the switch). A bundle's preset may
-- train from scratch (tabular): no base model, so both columns go null together.
ALTER TABLE task_presets ADD COLUMN enabled boolean NOT NULL DEFAULT true;
ALTER TABLE task_presets ALTER COLUMN base_model_repo DROP NOT NULL, ALTER COLUMN base_model_revision DROP NOT NULL;
ALTER TABLE task_presets ADD CONSTRAINT task_presets_base_model_check CHECK ((base_model_repo IS NULL) = (base_model_revision IS NULL));
-- A run on a bundle pins it next to package_sha256, the ray_training (core) zip the run used.
ALTER TABLE training_runs ADD COLUMN bundle_sha256 text CHECK (bundle_sha256 ~ '^[0-9a-f]{64}$');

-- migrate:down
ALTER TABLE training_runs DROP COLUMN bundle_sha256;
DELETE FROM task_presets WHERE base_model_repo IS NULL;
ALTER TABLE task_presets DROP CONSTRAINT task_presets_base_model_check;
ALTER TABLE task_presets ALTER COLUMN base_model_repo SET NOT NULL, ALTER COLUMN base_model_revision SET NOT NULL;
ALTER TABLE task_presets DROP COLUMN enabled;
ALTER TABLE task_types DROP COLUMN bundle_sha256;
DROP TABLE task_bundle_settings;
DROP TABLE job_package_audit;
DELETE FROM job_packages WHERE kind = 'task';
ALTER TABLE job_packages DROP CONSTRAINT job_packages_manifest_check;
ALTER TABLE job_packages DROP CONSTRAINT job_packages_name_check;
ALTER TABLE job_packages ADD CONSTRAINT job_packages_name_check CHECK (name IN ('ray_jobs', 'ray_training', 'ray_serving'));
ALTER TABLE job_packages DROP COLUMN manifest, DROP COLUMN kind;
