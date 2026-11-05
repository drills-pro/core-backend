-- migrate:up
-- The pure-Python job code (ray_jobs, ray_training) as rows: a content hash, where its zip lives in the staging bucket, and
-- whether runs may use it. A publisher (the admin API, CI later) adds a candidate; an admin promotes it to stable. Platform
-- data like task_presets: no tenant_id, so no RLS, and only /v1/admin routes write it.
-- A training run pins the hash it was created with, so a package promoted after that never changes it.
CREATE TABLE job_packages (
  name text NOT NULL CHECK (name IN ('ray_jobs', 'ray_training')),
  sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  s3_key text NOT NULL,
  status text NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate', 'stable', 'retired')),
  published_by text NOT NULL,
  updated_by text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (name, sha256)
);
GRANT SELECT, INSERT, UPDATE ON job_packages TO core_app;

ALTER TABLE training_runs ADD COLUMN package_sha256 text CHECK (package_sha256 ~ '^[0-9a-f]{64}$');

-- migrate:down
ALTER TABLE training_runs DROP COLUMN package_sha256;
DROP TABLE job_packages;
