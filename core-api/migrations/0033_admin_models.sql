-- migrate:up
-- Cross-tenant listing of registered models, for the back-office models
-- page (app/modules/admin/models).
--
-- model_versions, projects and deployments are all RLS-protected (0008,
-- 0002, 0007) and the admin routes set no `app.tenant_id`, so a plain
-- SELECT returns nothing. Same SECURITY DEFINER escape as admin_activity
-- (0032) and admin_tenant_projects (0030), granted to core_app only and
-- reached solely through routes behind get_platform_admin.
--
-- Two things are derived here rather than in the route, so that the
-- listing, its filters and its counts cannot disagree about them:
--
--   status   not a column — whatever the newest deployment of the run a
--            version came from says, and "never deployed" is a state too.
--   a model  not a table — `model_versions` is one row per *version*, so a
--            model is (project_id, mlflow_model_name) and the listing
--            shows its newest version with the rest folded behind it.

-- The listing orders by time across every tenant; model_versions_project_idx
-- (0008) leads with project_id and cannot serve that.
CREATE INDEX IF NOT EXISTS model_versions_created_at_idx
  ON model_versions(created_at DESC, id DESC);
-- Folding versions into their model, and expanding one model back into its
-- versions, both group on this pair.
CREATE INDEX IF NOT EXISTS model_versions_model_idx
  ON model_versions(project_id, mlflow_model_name, created_at DESC);
-- The per-version deployment lookup joins on training_run_id, which
-- deployments_project_idx (0007) does not lead with.
CREATE INDEX IF NOT EXISTS deployments_training_run_idx
  ON deployments(training_run_id, created_at DESC);

-- Dropped rather than replaced: these gained columns after first being
-- applied, and CREATE OR REPLACE cannot change a function's return type.
DROP FUNCTION IF EXISTS admin_models(int, int, uuid, text, text, text, text);
DROP FUNCTION IF EXISTS admin_model_stats();

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
  -- The newest version's id. It is what the page expands on, so it also
  -- identifies the model for admin_model_versions() below.
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
  -- deployed | deploying | stopped | not_deployed, as derived below.
  status text,
  created_at timestamptz,
  -- How many versions this model has, the newest one included.
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
             -- A deployment that failed or was torn down leaves the model
             -- registered and not serving, which is one state to an
             -- operator even though it is two rows to Ray.
             WHEN 'failed' THEN 'stopped'
             WHEN 'deleted' THEN 'stopped'
             ELSE 'not_deployed'
           END AS status,
           m.created_at
    FROM model_versions m
    JOIN projects p ON p.id = m.project_id AND p.deleted_at IS NULL
    JOIN tenants t ON t.id = m.tenant_id
    -- The newest deployment of the run this version came from. LEFT,
    -- because a registered model that was never deployed is the common
    -- case, not a missing row.
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
  -- One row per model: its newest version, carrying how many there are.
  -- Ordered by time rather than by mlflow_version, which is text — '10'
  -- sorts before '2' there, and registration order is what "newest" means
  -- anyway.
  latest AS (
    SELECT DISTINCT ON (m.project_id, m.model_name)
           m.*,
           -- Windows run before DISTINCT ON, so this counts every version
           -- of the model, not just the one row that survives.
           count(*) OVER (PARTITION BY m.project_id, m.model_name) AS version_count
    FROM matching m
    ORDER BY m.project_id, m.model_name, m.created_at DESC, m.id DESC
  )
  -- The status filter runs on the newest version, because that is the
  -- status the row reports; and it runs after the fold, so a model is
  -- never counted by an older version's state.
  SELECT l.*, count(*) OVER ()
  FROM latest l
  WHERE (p_status IS NULL OR l.status = p_status)
  ORDER BY l.created_at DESC, l.id DESC
  LIMIT p_limit OFFSET p_offset;
$$;
REVOKE ALL ON FUNCTION admin_models(int, int, uuid, text, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_models(int, int, uuid, text, text, text, text) TO core_app;

-- Every version of the model one listing row stands for, newest first —
-- what the page shows when that row is opened.
--
-- Identified by any one of its versions rather than by (project, name) so
-- the caller passes back the id it was given, with no second key to spell
-- and nothing to escape in a URL.
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
REVOKE ALL ON FUNCTION admin_model_versions(uuid, int, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_model_versions(uuid, int, int) TO core_app;

-- The totals behind the models page's stat strip and its tabs, counted
-- across every tenant. Aggregates only, and counted per *model* with the
-- same status vocabulary the listing folds onto it, so a tab's number and
-- the page it opens agree.
--
-- Two things the design's strip asks for are deliberately absent: how much
-- disk the artifacts take (nothing records an artifact's size — MLflow
-- holds the artifact and this schema only points at it) and how many
-- models await review (nothing records a review). The route leaves those
-- to the caller rather than inventing a number for them.
CREATE OR REPLACE FUNCTION admin_model_stats()
RETURNS TABLE(
  model_count bigint,
  deployed_count bigint,
  deploying_count bigint,
  stopped_count bigint,
  not_deployed_count bigint,
  project_count bigint,
  -- Every version of every model, which the model count no longer implies.
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
         -- How many projects those models are spread over, which is the
         -- line under the count.
         (SELECT count(DISTINCT project_id) FROM latest),
         (SELECT count(*) FROM versions);
$$;
REVOKE ALL ON FUNCTION admin_model_stats() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_model_stats() TO core_app;

-- migrate:down
DROP FUNCTION IF EXISTS admin_model_stats();
DROP FUNCTION IF EXISTS admin_model_versions(uuid, int, int);
DROP FUNCTION IF EXISTS admin_models(int, int, uuid, text, text, text, text);
DROP INDEX IF EXISTS deployments_training_run_idx;
DROP INDEX IF EXISTS model_versions_model_idx;
DROP INDEX IF EXISTS model_versions_created_at_idx;
