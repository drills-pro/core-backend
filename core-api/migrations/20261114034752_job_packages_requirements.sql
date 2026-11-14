-- migrate:up
-- Pinned Python requirements of a package or bundle (planning/26): the exact `name==version` pins Ray installs into the
-- job's `uv` runtime env on a stock image. Read from requirements.lock (runtime package) or the manifest (task bundle) at
-- register, validated by app/core/requirements.py. Empty = the job runs on the packages baked into the image, as before.
-- Platform data like job_packages (no tenant_id, no RLS); core_app's existing table grants cover the new column.
ALTER TABLE job_packages
  ADD COLUMN requirements jsonb NOT NULL DEFAULT '[]' CONSTRAINT job_packages_requirements_check CHECK (jsonb_typeof(requirements) = 'array');

-- migrate:down
ALTER TABLE job_packages DROP COLUMN requirements;
