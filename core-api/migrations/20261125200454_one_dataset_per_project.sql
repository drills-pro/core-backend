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
-- Restores the columns, the constraint and the functions the up step dropped or rewrote, as they stood before it.
DROP INDEX datasets_one_per_project;
ALTER TABLE datasets
  ADD COLUMN deleted_at timestamptz,
  ADD COLUMN published_version_id uuid REFERENCES dataset_versions(id),
  ADD COLUMN data_categories text[] NOT NULL DEFAULT '{}'
    CONSTRAINT datasets_data_categories_check CHECK (data_categories <@ ARRAY['personal_data', 'biometric', 'other_sensitive']);
CREATE UNIQUE INDEX datasets_one_live_per_project ON datasets(project_id) WHERE deleted_at IS NULL;

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
         (SELECT count(*) FROM datasets d WHERE d.project_id = p.id AND d.deleted_at IS NULL),
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
  WHERE d.tenant_id = p_tenant_id AND p.deleted_at IS NULL AND d.deleted_at IS NULL
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
         (SELECT count(*) FROM datasets d WHERE d.tenant_id = p_tenant_id AND d.deleted_at IS NULL),
         (SELECT count(*) FROM model_versions m WHERE m.tenant_id = p_tenant_id)
  FROM admin_tenant_stats(ARRAY[p_tenant_id]) s;
$$;


CREATE FUNCTION purgeable_datasets(p_cutoff timestamptz) RETURNS TABLE(tenant_id uuid, id uuid)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT d.tenant_id, d.id FROM datasets d WHERE d.deleted_at < p_cutoff;
$$;
REVOKE ALL ON FUNCTION purgeable_datasets(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION purgeable_datasets(timestamptz) TO core_app;


CREATE OR REPLACE FUNCTION purge_dataset_rows(p_dataset_id uuid, p_cutoff timestamptz) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  -- SECURITY DEFINER bypasses RLS, so the caller's tenant is checked here: a bug elsewhere in core_app
  -- cannot purge another tenant's dataset.
  PERFORM 1 FROM datasets
    WHERE id = p_dataset_id AND deleted_at < p_cutoff AND tenant_id = current_setting('app.tenant_id', true)::uuid
    FOR UPDATE;
  IF NOT FOUND THEN
    RETURN false;
  END IF;
  UPDATE datasets SET published_version_id = NULL WHERE id = p_dataset_id;
  UPDATE dataset_imports SET base_version_id = NULL WHERE dataset_id = p_dataset_id;
  UPDATE projects SET default_test_dataset_id = NULL WHERE default_test_dataset_id = p_dataset_id;
  UPDATE training_runs SET dataset_id = NULL WHERE dataset_id = p_dataset_id;
  UPDATE training_runs SET test_dataset_id = NULL WHERE test_dataset_id = p_dataset_id;
  UPDATE training_runs SET dataset_version_id = NULL
    WHERE dataset_version_id IN (SELECT id FROM dataset_versions WHERE dataset_id = p_dataset_id);
  DELETE FROM dataset_versions WHERE dataset_id = p_dataset_id;
  DELETE FROM dataset_imports WHERE dataset_id = p_dataset_id;
  DELETE FROM datasets WHERE id = p_dataset_id;
  RETURN true;
END;
$$;
REVOKE ALL ON FUNCTION purge_dataset_rows(uuid, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION purge_dataset_rows(uuid, timestamptz) TO core_app;


CREATE OR REPLACE FUNCTION list_public_project_datasets(p_project_id uuid)
RETURNS TABLE(id uuid, name text, published boolean, version_number integer, stats jsonb)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT d.id,
         CASE WHEN v.id IS NOT NULL AND p.sharing = 'gallery_downloads' THEN d.name ELSE NULL END,
         v.id IS NOT NULL AND p.sharing = 'gallery_downloads',
         CASE WHEN p.sharing = 'gallery_downloads' THEN v.number ELSE NULL END,
         CASE WHEN p.sharing = 'gallery_downloads' THEN v.stats ELSE NULL END
  FROM datasets d
  JOIN projects p ON p.id = d.project_id
  LEFT JOIN dataset_versions v ON v.id = d.published_version_id
  WHERE d.project_id = p_project_id AND d.deleted_at IS NULL
    AND p.sharing <> 'private' AND p.visibility = 'internal' AND p.status = 'active';
$$;
REVOKE ALL ON FUNCTION list_public_project_datasets(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION list_public_project_datasets(uuid) TO core_app;


