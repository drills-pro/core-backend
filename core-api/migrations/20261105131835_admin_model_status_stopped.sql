-- migrate:up
-- A stopped deployment (status added in 20261105125800) fell through to not_deployed in the admin views; it reads
-- as stopped, like failed and deleted. CREATE OR REPLACE keeps the existing grants.
CREATE OR REPLACE FUNCTION admin_models(
  p_limit int,
  p_offset int,
  p_tenant_id uuid,
  p_task_type text,
  p_visibility text,
  p_status text,
  p_q text
)
RETURNS TABLE(
  id uuid,
  tenant_id uuid,
  tenant_name text,
  project_id uuid,
  project_name text,
  task_type text,
  visibility text,
  model_name text,
  version text,
  stage text,
  status text,
  created_at timestamptz,
  version_count bigint,
  total bigint
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH matching AS (
    SELECT m.id, t.id AS tenant_id, t.name AS tenant_name,
           p.id AS project_id, p.name AS project_name, p.task_type, p.visibility,
           m.mlflow_model_name AS model_name, m.mlflow_version AS version,
           m.stage,
           CASE d.status
             WHEN 'active' THEN 'deployed'
             WHEN 'deploying' THEN 'deploying'
             WHEN 'stopped' THEN 'stopped'
             WHEN 'failed' THEN 'stopped'
             WHEN 'deleted' THEN 'stopped'
             ELSE 'not_deployed'
           END AS status,
           m.created_at
    FROM model_versions m
    JOIN projects p ON p.id = m.project_id AND p.deleted_at IS NULL
    JOIN tenants t ON t.id = m.tenant_id
    LEFT JOIN LATERAL (
      SELECT dep.status
      FROM deployments dep
      WHERE dep.training_run_id = m.training_run_id
      ORDER BY dep.created_at DESC
      LIMIT 1
    ) d ON true
    WHERE (p_tenant_id IS NULL OR m.tenant_id = p_tenant_id)
      AND (p_task_type IS NULL OR p.task_type = p_task_type)
      AND (p_visibility IS NULL OR p.visibility = p_visibility)
      AND (p_q IS NULL
           OR m.mlflow_model_name ILIKE '%' || p_q || '%'
           OR p.name ILIKE '%' || p_q || '%'
           OR t.name ILIKE '%' || p_q || '%')
  ),
  -- Newest by registration time, not mlflow_version (text: '10' sorts before '2'). Windows run
  -- before DISTINCT ON, so version_count counts every version.
  latest AS (
    SELECT DISTINCT ON (m.project_id, m.model_name)
           m.*,
           count(*) OVER (PARTITION BY m.project_id, m.model_name) AS version_count
    FROM matching m
    ORDER BY m.project_id, m.model_name, m.created_at DESC, m.id DESC
  )
  -- The status filter runs after the fold, on the status the row reports.
  SELECT l.*, count(*) OVER ()
  FROM latest l
  WHERE (p_status IS NULL OR l.status = p_status)
  ORDER BY l.created_at DESC, l.id DESC
  LIMIT p_limit OFFSET p_offset;
$$;

-- Every version of the model one listing row stands for, identified by any one of its versions.
CREATE OR REPLACE FUNCTION admin_model_versions(
  p_version_id uuid,
  p_limit int,
  p_offset int
)
RETURNS TABLE(
  id uuid,
  version text,
  stage text,
  status text,
  created_at timestamptz,
  total bigint
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH target AS (
    SELECT project_id, mlflow_model_name FROM model_versions WHERE id = p_version_id
  ),
  versions AS (
    SELECT m.id, m.mlflow_version AS version, m.stage,
           CASE d.status
             WHEN 'active' THEN 'deployed'
             WHEN 'deploying' THEN 'deploying'
             WHEN 'stopped' THEN 'stopped'
             WHEN 'failed' THEN 'stopped'
             WHEN 'deleted' THEN 'stopped'
             ELSE 'not_deployed'
           END AS status,
           m.created_at
    FROM model_versions m
    JOIN target t
      ON t.project_id = m.project_id AND t.mlflow_model_name = m.mlflow_model_name
    JOIN projects p ON p.id = m.project_id AND p.deleted_at IS NULL
    LEFT JOIN LATERAL (
      SELECT dep.status
      FROM deployments dep
      WHERE dep.training_run_id = m.training_run_id
      ORDER BY dep.created_at DESC
      LIMIT 1
    ) d ON true
  )
  SELECT v.*, count(*) OVER ()
  FROM versions v
  ORDER BY v.created_at DESC, v.id DESC
  LIMIT p_limit OFFSET p_offset;
$$;

-- Models page stat strip, counted per model with the listing's status vocabulary. Artifact size
-- and review state are not recorded anywhere, so they are not invented here.
CREATE OR REPLACE FUNCTION admin_model_stats()
RETURNS TABLE(
  model_count bigint,
  deployed_count bigint,
  deploying_count bigint,
  stopped_count bigint,
  not_deployed_count bigint,
  project_count bigint,
  version_count bigint
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH versions AS (
    SELECT m.id, m.project_id, m.mlflow_model_name, m.created_at,
           CASE d.status
             WHEN 'active' THEN 'deployed'
             WHEN 'deploying' THEN 'deploying'
             WHEN 'stopped' THEN 'stopped'
             WHEN 'failed' THEN 'stopped'
             WHEN 'deleted' THEN 'stopped'
             ELSE 'not_deployed'
           END AS status
    FROM model_versions m
    JOIN projects p ON p.id = m.project_id AND p.deleted_at IS NULL
    LEFT JOIN LATERAL (
      SELECT dep.status
      FROM deployments dep
      WHERE dep.training_run_id = m.training_run_id
      ORDER BY dep.created_at DESC
      LIMIT 1
    ) d ON true
  ),
  latest AS (
    SELECT DISTINCT ON (project_id, mlflow_model_name) project_id, status
    FROM versions
    ORDER BY project_id, mlflow_model_name, created_at DESC, id DESC
  )
  SELECT (SELECT count(*) FROM latest),
         (SELECT count(*) FROM latest WHERE status = 'deployed'),
         (SELECT count(*) FROM latest WHERE status = 'deploying'),
         (SELECT count(*) FROM latest WHERE status = 'stopped'),
         (SELECT count(*) FROM latest WHERE status = 'not_deployed'),
         (SELECT count(DISTINCT project_id) FROM latest),
         (SELECT count(*) FROM versions);
$$;

-- migrate:down
CREATE OR REPLACE FUNCTION admin_models(
  p_limit int,
  p_offset int,
  p_tenant_id uuid,
  p_task_type text,
  p_visibility text,
  p_status text,
  p_q text
)
RETURNS TABLE(
  id uuid,
  tenant_id uuid,
  tenant_name text,
  project_id uuid,
  project_name text,
  task_type text,
  visibility text,
  model_name text,
  version text,
  stage text,
  status text,
  created_at timestamptz,
  version_count bigint,
  total bigint
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH matching AS (
    SELECT m.id, t.id AS tenant_id, t.name AS tenant_name,
           p.id AS project_id, p.name AS project_name, p.task_type, p.visibility,
           m.mlflow_model_name AS model_name, m.mlflow_version AS version,
           m.stage,
           CASE d.status
             WHEN 'active' THEN 'deployed'
             WHEN 'deploying' THEN 'deploying'
             WHEN 'failed' THEN 'stopped'
             WHEN 'deleted' THEN 'stopped'
             ELSE 'not_deployed'
           END AS status,
           m.created_at
    FROM model_versions m
    JOIN projects p ON p.id = m.project_id AND p.deleted_at IS NULL
    JOIN tenants t ON t.id = m.tenant_id
    LEFT JOIN LATERAL (
      SELECT dep.status
      FROM deployments dep
      WHERE dep.training_run_id = m.training_run_id
      ORDER BY dep.created_at DESC
      LIMIT 1
    ) d ON true
    WHERE (p_tenant_id IS NULL OR m.tenant_id = p_tenant_id)
      AND (p_task_type IS NULL OR p.task_type = p_task_type)
      AND (p_visibility IS NULL OR p.visibility = p_visibility)
      AND (p_q IS NULL
           OR m.mlflow_model_name ILIKE '%' || p_q || '%'
           OR p.name ILIKE '%' || p_q || '%'
           OR t.name ILIKE '%' || p_q || '%')
  ),
  -- Newest by registration time, not mlflow_version (text: '10' sorts before '2'). Windows run
  -- before DISTINCT ON, so version_count counts every version.
  latest AS (
    SELECT DISTINCT ON (m.project_id, m.model_name)
           m.*,
           count(*) OVER (PARTITION BY m.project_id, m.model_name) AS version_count
    FROM matching m
    ORDER BY m.project_id, m.model_name, m.created_at DESC, m.id DESC
  )
  -- The status filter runs after the fold, on the status the row reports.
  SELECT l.*, count(*) OVER ()
  FROM latest l
  WHERE (p_status IS NULL OR l.status = p_status)
  ORDER BY l.created_at DESC, l.id DESC
  LIMIT p_limit OFFSET p_offset;
$$;

-- Every version of the model one listing row stands for, identified by any one of its versions.
CREATE OR REPLACE FUNCTION admin_model_versions(
  p_version_id uuid,
  p_limit int,
  p_offset int
)
RETURNS TABLE(
  id uuid,
  version text,
  stage text,
  status text,
  created_at timestamptz,
  total bigint
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH target AS (
    SELECT project_id, mlflow_model_name FROM model_versions WHERE id = p_version_id
  ),
  versions AS (
    SELECT m.id, m.mlflow_version AS version, m.stage,
           CASE d.status
             WHEN 'active' THEN 'deployed'
             WHEN 'deploying' THEN 'deploying'
             WHEN 'failed' THEN 'stopped'
             WHEN 'deleted' THEN 'stopped'
             ELSE 'not_deployed'
           END AS status,
           m.created_at
    FROM model_versions m
    JOIN target t
      ON t.project_id = m.project_id AND t.mlflow_model_name = m.mlflow_model_name
    JOIN projects p ON p.id = m.project_id AND p.deleted_at IS NULL
    LEFT JOIN LATERAL (
      SELECT dep.status
      FROM deployments dep
      WHERE dep.training_run_id = m.training_run_id
      ORDER BY dep.created_at DESC
      LIMIT 1
    ) d ON true
  )
  SELECT v.*, count(*) OVER ()
  FROM versions v
  ORDER BY v.created_at DESC, v.id DESC
  LIMIT p_limit OFFSET p_offset;
$$;

-- Models page stat strip, counted per model with the listing's status vocabulary. Artifact size
-- and review state are not recorded anywhere, so they are not invented here.
CREATE OR REPLACE FUNCTION admin_model_stats()
RETURNS TABLE(
  model_count bigint,
  deployed_count bigint,
  deploying_count bigint,
  stopped_count bigint,
  not_deployed_count bigint,
  project_count bigint,
  version_count bigint
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH versions AS (
    SELECT m.id, m.project_id, m.mlflow_model_name, m.created_at,
           CASE d.status
             WHEN 'active' THEN 'deployed'
             WHEN 'deploying' THEN 'deploying'
             WHEN 'failed' THEN 'stopped'
             WHEN 'deleted' THEN 'stopped'
             ELSE 'not_deployed'
           END AS status
    FROM model_versions m
    JOIN projects p ON p.id = m.project_id AND p.deleted_at IS NULL
    LEFT JOIN LATERAL (
      SELECT dep.status
      FROM deployments dep
      WHERE dep.training_run_id = m.training_run_id
      ORDER BY dep.created_at DESC
      LIMIT 1
    ) d ON true
  ),
  latest AS (
    SELECT DISTINCT ON (project_id, mlflow_model_name) project_id, status
    FROM versions
    ORDER BY project_id, mlflow_model_name, created_at DESC, id DESC
  )
  SELECT (SELECT count(*) FROM latest),
         (SELECT count(*) FROM latest WHERE status = 'deployed'),
         (SELECT count(*) FROM latest WHERE status = 'deploying'),
         (SELECT count(*) FROM latest WHERE status = 'stopped'),
         (SELECT count(*) FROM latest WHERE status = 'not_deployed'),
         (SELECT count(DISTINCT project_id) FROM latest),
         (SELECT count(*) FROM versions);
$$;
