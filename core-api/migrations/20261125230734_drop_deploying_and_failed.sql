-- migrate:up
-- A deployment is active as soon as it exists: nothing follows a Ray job, so no row is 'deploying', and the legacy serving
-- that wrote 'failed' is gone. The status CHECK and the indexes that named those values lose them. The guard refuses to run
-- while a row still holds one, rather than rewriting it.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM deployments WHERE status IN ('deploying', 'failed')) THEN
    RAISE EXCEPTION 'deployments still hold deploying or failed rows; move them before this migration';
  END IF;
END
$$;

DROP INDEX deployments_deploying_idx;
DROP INDEX deployments_one_active_per_version;
CREATE UNIQUE INDEX deployments_one_active_per_version ON deployments(model_version_id) WHERE status = 'active';
DROP INDEX deployments_one_live_per_run;
CREATE UNIQUE INDEX deployments_one_live_per_run ON deployments(training_run_id) WHERE status IN ('active', 'stopped');
ALTER TABLE deployments DROP CONSTRAINT deployments_status_check;
ALTER TABLE deployments ADD CONSTRAINT deployments_status_check CHECK (status IN ('active', 'stopped', 'deleted'));
-- Nothing sweeps in-flight deployments any more (no row can be in flight), so the sweep list goes with them.
DROP FUNCTION inflight_deploy_tenant_ids();

-- The admin functions drop their deploying and failed branches; the stats function loses its deploying_count column,
-- which changes its return type, so it is dropped and created again.
CREATE OR REPLACE FUNCTION admin_models(
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
             WHEN 'stopped' THEN 'stopped'
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
CREATE OR REPLACE FUNCTION admin_model_versions(
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
             WHEN 'stopped' THEN 'stopped'
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
DROP FUNCTION admin_model_stats();
CREATE FUNCTION admin_model_stats()
RETURNS TABLE(
  model_count bigint,
  deployed_count bigint,
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
             WHEN 'stopped' THEN 'stopped'
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
         (SELECT count(*) FROM latest WHERE status = 'stopped'),
         (SELECT count(*) FROM latest WHERE status = 'not_deployed'),
         (SELECT count(DISTINCT project_id) FROM latest),
         (SELECT count(*) FROM versions);
$$;
REVOKE ALL ON FUNCTION admin_model_stats() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_model_stats() TO core_app;
REVOKE ALL ON FUNCTION admin_models(int, timestamptz, uuid, uuid, text, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_models(int, timestamptz, uuid, uuid, text, text, text, text) TO core_app;
REVOKE ALL ON FUNCTION admin_model_versions(uuid, int, timestamptz, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_model_versions(uuid, int, timestamptz, uuid) TO core_app;

-- migrate:down
DROP INDEX deployments_one_active_per_version;
DROP INDEX deployments_one_live_per_run;
ALTER TABLE deployments DROP CONSTRAINT deployments_status_check;
ALTER TABLE deployments ADD CONSTRAINT deployments_status_check
  CHECK (status IN ('deploying', 'active', 'stopped', 'failed', 'deleted'));
CREATE UNIQUE INDEX deployments_one_active_per_version ON deployments(model_version_id) WHERE status IN ('deploying', 'active');
CREATE UNIQUE INDEX deployments_one_live_per_run ON deployments(training_run_id) WHERE status IN ('deploying', 'active', 'stopped');
CREATE INDEX deployments_deploying_idx ON deployments(tenant_id) WHERE status = 'deploying';
CREATE OR REPLACE FUNCTION inflight_deploy_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT DISTINCT tenant_id FROM deployments WHERE status = 'deploying';
$$;

DROP FUNCTION admin_model_stats();
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
REVOKE ALL ON FUNCTION admin_model_stats() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_model_stats() TO core_app;

CREATE OR REPLACE FUNCTION admin_models(
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
CREATE OR REPLACE FUNCTION admin_model_versions(
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
