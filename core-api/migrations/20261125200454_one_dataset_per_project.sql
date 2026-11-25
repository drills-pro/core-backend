-- migrate:up
-- A project has one dataset: its versions and imports all hang off it, so a second live one would split the history.
-- Cut for now, with nothing reading them: soft delete and restore (the Trash, the 7-day purge), publish and unpublish,
-- and the declared data categories. A project's delete covers its dataset.

DROP FUNCTION purgeable_datasets(timestamptz);
DROP FUNCTION purge_dataset_rows(uuid, timestamptz);
DROP FUNCTION list_public_project_datasets(uuid);

-- The back-office views stop reading the soft-delete column before it goes.
CREATE OR REPLACE FUNCTION admin_tenant_projects(p_tenant_id uuid)
RETURNS TABLE(
  id uuid,
  name text,
  status text,
  task_type text,
  visibility text,
  created_at timestamptz,
  dataset_count bigint,
  model_count bigint,
  model_name text,
  model_version text
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT p.id, p.name, p.status, p.task_type, p.visibility, p.created_at,
         (SELECT count(*) FROM datasets d WHERE d.project_id = p.id),
         (SELECT count(*) FROM model_versions m WHERE m.project_id = p.id),
         -- The newest registered version is what the card names.
         latest.mlflow_model_name,
         latest.mlflow_version
  FROM projects p
  LEFT JOIN LATERAL (
    SELECT m.mlflow_model_name, m.mlflow_version
    FROM model_versions m
    WHERE m.project_id = p.id
    ORDER BY m.created_at DESC
    LIMIT 1
  ) latest ON true
  WHERE p.tenant_id = p_tenant_id AND p.deleted_at IS NULL
  ORDER BY p.created_at DESC;
$$;

CREATE OR REPLACE FUNCTION admin_tenant_datasets(p_tenant_id uuid)
RETURNS TABLE(
  id uuid,
  name text,
  project_id uuid,
  project_name text,
  import_status text,
  created_at timestamptz
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT d.id, d.name, p.id, p.name,
         -- Null for a dataset with no import yet; the newest import is the one that counts.
         (SELECT i.status FROM dataset_imports i
           WHERE i.dataset_id = d.id ORDER BY i.created_at DESC LIMIT 1),
         d.created_at
  FROM datasets d
  JOIN projects p ON p.id = d.project_id
  WHERE d.tenant_id = p_tenant_id AND p.deleted_at IS NULL
  ORDER BY d.created_at DESC;
$$;

CREATE OR REPLACE FUNCTION admin_tenant_detail_stats(p_tenant_id uuid)
RETURNS TABLE(
  project_count bigint,
  member_count bigint,
  dataset_count bigint,
  model_count bigint
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT s.project_count, s.member_count,
         (SELECT count(*) FROM datasets d WHERE d.tenant_id = p_tenant_id),
         (SELECT count(*) FROM model_versions m WHERE m.tenant_id = p_tenant_id)
  FROM admin_tenant_stats(ARRAY[p_tenant_id]) s;
$$;

-- migration-lint: allow-destructive (cut for now - nothing reads this column and soft-deleted rows are gone)
ALTER TABLE datasets DROP COLUMN deleted_at;
-- migration-lint: allow-destructive (cut for now - publish is removed and the pointer has no reader)
ALTER TABLE datasets DROP COLUMN published_version_id;
-- migration-lint: allow-destructive (cut for now - stored only, nothing reads it)
ALTER TABLE datasets DROP COLUMN data_categories;

CREATE UNIQUE INDEX datasets_one_per_project ON datasets(project_id);

-- migrate:down
-- Restores the columns and the constraint only. The dropped functions come back from their earlier migrations if needed.
DROP INDEX datasets_one_per_project;
ALTER TABLE datasets
  ADD COLUMN deleted_at timestamptz,
  ADD COLUMN published_version_id uuid REFERENCES dataset_versions(id),
  ADD COLUMN data_categories text[] NOT NULL DEFAULT '{}'
    CONSTRAINT datasets_data_categories_check CHECK (data_categories <@ ARRAY['personal_data', 'biometric', 'other_sensitive']);
CREATE UNIQUE INDEX datasets_one_live_per_project ON datasets(project_id) WHERE deleted_at IS NULL;
