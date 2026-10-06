-- Cross-tenant listing of projects, for the back-office projects page
-- (app/modules/admin/projects).
--
-- projects, datasets, model_versions and deployments are all RLS-protected
-- (0002, 0006, 0008, 0007) and the admin routes set no `app.tenant_id`, so
-- a plain SELECT returns nothing. Same SECURITY DEFINER escape as
-- admin_models (0030) and admin_tenant_projects (0027), granted to core_app
-- only and reached solely through routes behind get_platform_admin.
--
-- deployment_status is derived, in the same vocabulary as admin_models, but
-- over the whole project rather than one run: a project is "deployed" while
-- any of its deployments is serving, since that is what an operator asks of
-- a project — is anything of it live?

DROP FUNCTION IF EXISTS admin_projects(int, int, uuid, text, text, text);
DROP FUNCTION IF EXISTS admin_project_stats();

CREATE OR REPLACE FUNCTION admin_projects(
  p_limit int,
  p_offset int,
  p_tenant_id uuid,
  p_task_type text,
  p_visibility text,
  p_q text
)
RETURNS TABLE(
  id uuid,
  tenant_id uuid,
  tenant_name text,
  name text,
  -- active | archived | pending_delete (projects.status).
  status text,
  task_type text,
  visibility text,
  created_at timestamptz,
  dataset_count bigint,
  -- Distinct models, not versions: the same fold admin_models makes.
  model_count bigint,
  version_count bigint,
  -- The newest registered version, which the row names; null when none.
  latest_model_name text,
  latest_model_version text,
  -- deployed | deploying | stopped | not_deployed, as derived below.
  deployment_status text,
  total bigint
)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT p.id, t.id, t.name, p.name, p.status, p.task_type, p.visibility, p.created_at,
         (SELECT count(*) FROM datasets d WHERE d.project_id = p.id),
         (SELECT count(DISTINCT m.mlflow_model_name) FROM model_versions m WHERE m.project_id = p.id),
         (SELECT count(*) FROM model_versions m WHERE m.project_id = p.id),
         latest.mlflow_model_name,
         latest.mlflow_version,
         CASE
           WHEN dep.active THEN 'deployed'
           WHEN dep.deploying THEN 'deploying'
           -- Deployed once, failed or torn down since: not serving, but
           -- not the same as never having tried.
           WHEN dep.attempted THEN 'stopped'
           ELSE 'not_deployed'
         END,
         count(*) OVER ()
  FROM projects p
  JOIN tenants t ON t.id = p.tenant_id
  LEFT JOIN LATERAL (
    SELECT m.mlflow_model_name, m.mlflow_version
    FROM model_versions m
    WHERE m.project_id = p.id
    ORDER BY m.created_at DESC, m.id DESC
    LIMIT 1
  ) latest ON true
  CROSS JOIN LATERAL (
    SELECT bool_or(d.status = 'active') AS active,
           bool_or(d.status = 'deploying') AS deploying,
           count(*) > 0 AS attempted
    FROM deployments d
    WHERE d.project_id = p.id
  ) dep
  WHERE p.deleted_at IS NULL
    AND (p_tenant_id IS NULL OR p.tenant_id = p_tenant_id)
    AND (p_task_type IS NULL OR p.task_type = p_task_type)
    AND (p_visibility IS NULL OR p.visibility = p_visibility)
    AND (p_q IS NULL OR p.name ILIKE '%' || p_q || '%' OR t.name ILIKE '%' || p_q || '%')
  ORDER BY p.created_at DESC, p.id DESC
  LIMIT p_limit OFFSET p_offset;
$$;
REVOKE ALL ON FUNCTION admin_projects(int, int, uuid, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_projects(int, int, uuid, text, text, text) TO core_app;

-- The totals behind the projects page's stat strip, counted across every
-- tenant so they do not move when a filter does.
CREATE OR REPLACE FUNCTION admin_project_stats()
RETURNS TABLE(
  project_count bigint,
  -- How many organisations those projects are spread over.
  tenant_count bigint,
  -- Projects with at least one deployment serving now.
  deployed_count bigint,
  dataset_count bigint,
  model_count bigint
)
LANGUAGE sql SECURITY DEFINER AS $$
  WITH live AS (
    SELECT id, tenant_id FROM projects WHERE deleted_at IS NULL
  )
  SELECT (SELECT count(*) FROM live),
         (SELECT count(DISTINCT tenant_id) FROM live),
         (SELECT count(DISTINCT d.project_id) FROM deployments d
            JOIN live l ON l.id = d.project_id WHERE d.status = 'active'),
         (SELECT count(*) FROM datasets d JOIN live l ON l.id = d.project_id),
         (SELECT count(*) FROM (
            SELECT DISTINCT m.project_id, m.mlflow_model_name
            FROM model_versions m JOIN live l ON l.id = m.project_id
          ) models);
$$;
REVOKE ALL ON FUNCTION admin_project_stats() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_project_stats() TO core_app;
