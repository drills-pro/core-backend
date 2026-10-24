-- migrate:up
-- Datasets are soft-deleted (deleted_at) for 7 days before the purge sweep; the back-office views must
-- not show or count them, or they disagree with the tenant's own list. import_status is now the import
-- lifecycle status (docs/datasets-plan.md decision 10), not a Hub import state.

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

-- migrate:down
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
