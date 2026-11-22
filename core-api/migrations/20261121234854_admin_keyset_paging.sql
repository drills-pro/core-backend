-- migrate:up
-- The admin listings page by keyset, like every other list: (created_at, id) < the cursor instead of OFFSET.
-- `total` stays count(*) OVER (), taken over the filtered set before the cursor cuts the page, so the pager's
-- X-Total-Count does not move as you page. New signatures, so the old (limit, offset) ones are dropped.
-- Keyset pages need the id tie-breaker in the index as well as in the ORDER BY.
CREATE INDEX IF NOT EXISTS teams_tenant_created_id_idx ON teams (tenant_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS usage_events_project_occurred_id_idx ON usage_events (project_id, occurred_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS training_runs_project_created_id_idx ON training_runs (project_id, created_at DESC, id DESC);
DROP FUNCTION admin_activity(int, int, uuid, text, text, timestamptz);
DROP FUNCTION admin_models(int, int, uuid, text, text, text, text);
DROP FUNCTION admin_model_versions(uuid, int, int);

CREATE FUNCTION admin_activity(
  p_limit int,
  p_after_ts timestamptz,
  p_after_id uuid,
  p_tenant_id uuid,
  p_entity_type text,
  p_q text,
  p_since timestamptz
)
RETURNS TABLE(
  id uuid,
  tenant_id uuid,
  tenant_name text,
  project_id uuid,
  project_name text,
  actor text,
  action text,
  entity_type text,
  entity_id text,
  created_at timestamptz,
  total bigint
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH matching AS (
    SELECT a.id, a.tenant_id, t.name AS tenant_name,
           a.project_id, p.name AS project_name,
           a.actor, a.action, a.entity_type, a.entity_id, a.created_at
    FROM audit_log a
    JOIN tenants t ON t.id = a.tenant_id
    -- LEFT: a project that went away should not take its history with it.
    LEFT JOIN projects p ON p.id = a.project_id
    WHERE (p_tenant_id IS NULL OR a.tenant_id = p_tenant_id)
      AND (p_entity_type IS NULL OR a.entity_type = p_entity_type)
      AND (p_since IS NULL OR a.created_at >= p_since)
      AND (p_q IS NULL
           OR a.actor ILIKE '%' || p_q || '%'
           OR a.action ILIKE '%' || p_q || '%'
           OR a.entity_type ILIKE '%' || p_q || '%'
           OR a.entity_id ILIKE '%' || p_q || '%'
           OR t.name ILIKE '%' || p_q || '%'
           OR p.name ILIKE '%' || p_q || '%')
  )
  -- total is counted over the whole filtered set; the cursor cuts the page out of it afterwards.
  SELECT s.*
  FROM (SELECT m.*, count(*) OVER () AS total FROM matching m) s
  WHERE p_after_ts IS NULL OR (s.created_at, s.id) < (p_after_ts, p_after_id)
  ORDER BY s.created_at DESC, s.id DESC
  LIMIT p_limit;
$$;
CREATE FUNCTION admin_models(
  p_limit int,
  p_after_ts timestamptz,
  p_after_id uuid,
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
  SELECT s.*
  FROM (SELECT l.*, count(*) OVER () AS total FROM latest l WHERE (p_status IS NULL OR l.status = p_status)) s
  WHERE p_after_ts IS NULL OR (s.created_at, s.id) < (p_after_ts, p_after_id)
  ORDER BY s.created_at DESC, s.id DESC
  LIMIT p_limit;
$$;
CREATE FUNCTION admin_model_versions(
  p_version_id uuid,
  p_limit int,
  p_after_ts timestamptz,
  p_after_id uuid
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
  SELECT s.*
  FROM (SELECT v.*, count(*) OVER () AS total FROM versions v) s
  WHERE p_after_ts IS NULL OR (s.created_at, s.id) < (p_after_ts, p_after_id)
  ORDER BY s.created_at DESC, s.id DESC
  LIMIT p_limit;
$$;

REVOKE ALL ON FUNCTION admin_activity(int, timestamptz, uuid, uuid, text, text, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_activity(int, timestamptz, uuid, uuid, text, text, timestamptz) TO core_app;
REVOKE ALL ON FUNCTION admin_models(int, timestamptz, uuid, uuid, text, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_models(int, timestamptz, uuid, uuid, text, text, text, text) TO core_app;
REVOKE ALL ON FUNCTION admin_model_versions(uuid, int, timestamptz, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_model_versions(uuid, int, timestamptz, uuid) TO core_app;

-- migrate:down
DROP FUNCTION admin_activity(int, timestamptz, uuid, uuid, text, text, timestamptz);
DROP FUNCTION admin_models(int, timestamptz, uuid, uuid, text, text, text, text);
DROP FUNCTION admin_model_versions(uuid, int, timestamptz, uuid);

CREATE FUNCTION admin_activity(
  p_limit int,
  p_offset int,
  p_tenant_id uuid,
  p_entity_type text,
  p_q text,
  p_since timestamptz
)
RETURNS TABLE(
  id uuid,
  tenant_id uuid,
  tenant_name text,
  project_id uuid,
  project_name text,
  actor text,
  action text,
  entity_type text,
  entity_id text,
  created_at timestamptz,
  total bigint
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH matching AS (
    SELECT a.id, a.tenant_id, t.name AS tenant_name,
           a.project_id, p.name AS project_name,
           a.actor, a.action, a.entity_type, a.entity_id, a.created_at
    FROM audit_log a
    JOIN tenants t ON t.id = a.tenant_id
    -- LEFT: a project that went away should not take its history with it.
    LEFT JOIN projects p ON p.id = a.project_id
    WHERE (p_tenant_id IS NULL OR a.tenant_id = p_tenant_id)
      AND (p_entity_type IS NULL OR a.entity_type = p_entity_type)
      AND (p_since IS NULL OR a.created_at >= p_since)
      AND (p_q IS NULL
           OR a.actor ILIKE '%' || p_q || '%'
           OR a.action ILIKE '%' || p_q || '%'
           OR a.entity_type ILIKE '%' || p_q || '%'
           OR a.entity_id ILIKE '%' || p_q || '%'
           OR t.name ILIKE '%' || p_q || '%'
           OR p.name ILIKE '%' || p_q || '%')
  )
  SELECT m.*, count(*) OVER ()
  FROM matching m
  ORDER BY m.created_at DESC, m.id DESC
  LIMIT p_limit OFFSET p_offset;
$$;
CREATE FUNCTION admin_models(
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
CREATE FUNCTION admin_model_versions(
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

REVOKE ALL ON FUNCTION admin_activity(int, int, uuid, text, text, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_activity(int, int, uuid, text, text, timestamptz) TO core_app;
REVOKE ALL ON FUNCTION admin_models(int, int, uuid, text, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_models(int, int, uuid, text, text, text, text) TO core_app;
REVOKE ALL ON FUNCTION admin_model_versions(uuid, int, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_model_versions(uuid, int, int) TO core_app;
