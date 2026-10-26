-- migrate:up
-- Datasets rebuilt around immutable versions and import jobs (docs/datasets-plan.md). No production
-- data exists, so dataset_imports is recreated rather than reshaped; labeling and preprocessing go.

-- migration-lint: allow-destructive (labeling and preprocessing are removed, no production data)
DROP TABLE labeling_projects;
-- migration-lint: allow-destructive (preprocessing is removed, no production data)
DROP TABLE dataset_preprocessing_jobs;
-- migration-lint: allow-destructive (recreated below with the import lifecycle, no production data)
DROP TABLE dataset_imports;
DROP FUNCTION inflight_preprocessing_tenant_ids();

CREATE TABLE dataset_imports (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id),
  project_id      uuid NOT NULL REFERENCES projects(id),
  dataset_id      uuid NOT NULL REFERENCES datasets(id),
  source          text NOT NULL CONSTRAINT dataset_imports_source_check CHECK (source IN ('upload', 'huggingface')),
  status          text NOT NULL DEFAULT 'awaiting_upload' CONSTRAINT dataset_imports_status_check CHECK (status IN (
                    'awaiting_upload', 'queued', 'running', 'awaiting_review', 'committing', 'succeeded',
                    'failed', 'cancelled', 'expired', 'discarded')),
  -- Merge on top of this version; FK added once dataset_versions exists.
  base_version_id uuid,
  params          jsonb NOT NULL DEFAULT '{}'::jsonb,
  report          jsonb,
  staging_key     text,
  ray_job_id      text,
  expires_at      timestamptz,
  idempotency_key text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  completed_at    timestamptz,
  UNIQUE (dataset_id, idempotency_key)
);
CREATE INDEX dataset_imports_dataset_idx ON dataset_imports(dataset_id, created_at DESC);
CREATE INDEX dataset_imports_active_idx ON dataset_imports(tenant_id)
  WHERE status IN ('awaiting_upload', 'queued', 'running', 'awaiting_review', 'committing');
CREATE INDEX dataset_imports_expiry_idx ON dataset_imports(expires_at)
  WHERE status IN ('awaiting_upload', 'awaiting_review');

CREATE TABLE dataset_versions (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  dataset_id    uuid NOT NULL REFERENCES datasets(id),
  number        integer NOT NULL CHECK (number > 0),
  lakefs_commit text NOT NULL,
  import_id     uuid NOT NULL REFERENCES dataset_imports(id),
  stats         jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (dataset_id, number)
);
ALTER TABLE dataset_imports ADD FOREIGN KEY (base_version_id) REFERENCES dataset_versions(id);

-- migration-lint: allow-destructive (replaced by published_version_id and the lakeFS repo per dataset)
ALTER TABLE datasets
  DROP COLUMN s3_prefix, DROP COLUMN published, DROP COLUMN published_ref,
  ADD COLUMN deleted_at timestamptz,
  ADD COLUMN published_version_id uuid REFERENCES dataset_versions(id);

ALTER TABLE dataset_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE dataset_imports ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON dataset_versions USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation ON dataset_imports USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT, UPDATE ON dataset_imports TO core_app;
-- Versions are immutable.
GRANT SELECT, INSERT ON dataset_versions TO core_app;

-- Reconcilers find tenants with running work before setting app.tenant_id.
CREATE OR REPLACE FUNCTION inflight_import_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT DISTINCT tenant_id FROM dataset_imports WHERE status IN ('queued', 'running', 'committing');
$$;

-- Metadata and stats only, never file access. A dataset that is not published lists without its name.
DROP FUNCTION list_public_project_datasets(uuid);
CREATE FUNCTION list_public_project_datasets(p_project_id uuid)
RETURNS TABLE(id uuid, name text, published boolean, version_number integer, stats jsonb)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT d.id,
         CASE WHEN v.id IS NOT NULL THEN d.name ELSE NULL END,
         v.id IS NOT NULL,
         v.number,
         v.stats
  FROM datasets d
  JOIN projects p ON p.id = d.project_id
  LEFT JOIN dataset_versions v ON v.id = d.published_version_id
  WHERE d.project_id = p_project_id AND d.deleted_at IS NULL
    AND p.gallery_published AND p.visibility = 'internal' AND p.status = 'active';
$$;
REVOKE ALL ON FUNCTION list_public_project_datasets(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION list_public_project_datasets(uuid) TO core_app;

-- migrate:down
DROP FUNCTION list_public_project_datasets(uuid);
DROP TABLE dataset_versions, dataset_imports CASCADE;
ALTER TABLE datasets
  ADD COLUMN s3_prefix text NOT NULL DEFAULT '', ADD COLUMN published boolean NOT NULL DEFAULT false,
  ADD COLUMN published_ref text, DROP COLUMN deleted_at, DROP COLUMN published_version_id;
ALTER TABLE datasets ALTER COLUMN s3_prefix DROP DEFAULT;

CREATE TABLE labeling_projects (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id),
  project_id      uuid NOT NULL REFERENCES projects(id),
  dataset_id      uuid NOT NULL REFERENCES datasets(id),
  label_studio_id integer NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX labeling_projects_project_idx ON labeling_projects(project_id, created_at DESC);

CREATE TABLE dataset_imports (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL REFERENCES tenants(id),
  project_id   uuid NOT NULL REFERENCES projects(id),
  dataset_id   uuid NOT NULL REFERENCES datasets(id),
  ray_job_id   text NOT NULL,
  hf_dataset   text NOT NULL,
  kind         text NOT NULL CHECK (kind IN ('images', 'tabular')),
  status       text NOT NULL DEFAULT 'submitted' CHECK (status IN ('submitted', 'running', 'succeeded', 'failed')),
  error        text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);
CREATE INDEX dataset_imports_dataset_idx ON dataset_imports(dataset_id, created_at DESC);
CREATE INDEX dataset_imports_inflight_idx ON dataset_imports(tenant_id) WHERE status IN ('submitted', 'running');

CREATE TABLE dataset_preprocessing_jobs (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL REFERENCES tenants(id),
  project_id   uuid NOT NULL REFERENCES projects(id),
  dataset_id   uuid NOT NULL REFERENCES datasets(id),
  ray_job_id   text NOT NULL,
  config       jsonb NOT NULL,
  status       text NOT NULL DEFAULT 'submitted' CHECK (status IN ('submitted', 'running', 'succeeded', 'failed')),
  output_ref   text,
  error        text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);
CREATE INDEX dataset_preprocessing_jobs_dataset_idx ON dataset_preprocessing_jobs(dataset_id, created_at DESC);

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['labeling_projects', 'dataset_imports', 'dataset_preprocessing_jobs']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_setting(''app.tenant_id'', true)::uuid)', t);
  END LOOP;
END
$$;
GRANT SELECT, INSERT, UPDATE ON dataset_imports, dataset_preprocessing_jobs TO core_app;
GRANT SELECT, INSERT ON labeling_projects TO core_app;

CREATE OR REPLACE FUNCTION inflight_import_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT DISTINCT tenant_id FROM dataset_imports WHERE status IN ('submitted', 'running');
$$;
CREATE FUNCTION inflight_preprocessing_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT DISTINCT tenant_id FROM dataset_preprocessing_jobs WHERE status IN ('submitted', 'running');
$$;
REVOKE ALL ON FUNCTION inflight_preprocessing_tenant_ids() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION inflight_preprocessing_tenant_ids() TO core_app;

CREATE FUNCTION list_public_project_datasets(p_project_id uuid)
RETURNS TABLE(id uuid, name text, published boolean, published_ref text)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT d.id,
         CASE WHEN d.published THEN d.name ELSE NULL END,
         d.published,
         CASE WHEN d.published THEN d.published_ref ELSE NULL END
  FROM datasets d
  JOIN projects p ON p.id = d.project_id
  WHERE d.project_id = p_project_id AND p.gallery_published AND p.visibility = 'internal' AND p.status = 'active';
$$;
REVOKE ALL ON FUNCTION list_public_project_datasets(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION list_public_project_datasets(uuid) TO core_app;
