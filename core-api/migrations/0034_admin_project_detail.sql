-- One project as the back-office sees it (app/modules/admin/projects): the
-- detail page behind a row of the /v1/admin/projects listing (0033).
--
-- Same SECURITY DEFINER escape as admin_projects and the tenant detail
-- readers (0027): every table read here is RLS-protected and the admin
-- connection sets no `app.tenant_id`. Each function is pinned to the one
-- project the caller asked for, and soft-deleted projects answer as absent.

CREATE OR REPLACE FUNCTION admin_project(p_project_id uuid)
RETURNS TABLE(
  id uuid,
  tenant_id uuid,
  tenant_name text,
  -- For the route's Zitadel lookups (members); not shown on the page.
  zitadel_org_id text,
  name text,
  status text,
  task_type text,
  visibility text,
  created_at timestamptz,
  dataset_count bigint,
  -- Distinct models, versions folded — as admin_projects counts them.
  model_count bigint,
  version_count bigint,
  member_count bigint,
  deployment_count bigint
)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT p.id, t.id, t.name, t.zitadel_org_id, p.name, p.status, p.task_type, p.visibility,
         p.created_at,
         (SELECT count(*) FROM datasets d WHERE d.project_id = p.id),
         (SELECT count(DISTINCT m.mlflow_model_name) FROM model_versions m WHERE m.project_id = p.id),
         (SELECT count(*) FROM model_versions m WHERE m.project_id = p.id),
         (SELECT count(*) FROM project_members pm WHERE pm.project_id = p.id),
         (SELECT count(*) FROM deployments d WHERE d.project_id = p.id)
  FROM projects p
  JOIN tenants t ON t.id = p.tenant_id
  WHERE p.id = p_project_id AND p.deleted_at IS NULL;
$$;
REVOKE ALL ON FUNCTION admin_project(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_project(uuid) TO core_app;

-- Every registered version in the project, newest first, with the same
-- derived status admin_models (0030) gives a version: whatever the newest
-- deployment of the run it came from says.
CREATE OR REPLACE FUNCTION admin_project_models(p_project_id uuid)
RETURNS TABLE(
  id uuid,
  model_name text,
  version text,
  stage text,
  status text,
  created_at timestamptz
)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT m.id, m.mlflow_model_name, m.mlflow_version, m.stage,
         CASE d.status
           WHEN 'active' THEN 'deployed'
           WHEN 'deploying' THEN 'deploying'
           WHEN 'failed' THEN 'stopped'
           WHEN 'deleted' THEN 'stopped'
           ELSE 'not_deployed'
         END,
         m.created_at
  FROM model_versions m
  JOIN projects p ON p.id = m.project_id AND p.deleted_at IS NULL
  LEFT JOIN LATERAL (
    SELECT dep.status
    FROM deployments dep
    WHERE dep.training_run_id = m.training_run_id
    ORDER BY dep.created_at DESC
    LIMIT 1
  ) d ON true
  WHERE m.project_id = p_project_id
  ORDER BY m.created_at DESC, m.id DESC;
$$;
REVOKE ALL ON FUNCTION admin_project_models(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_project_models(uuid) TO core_app;

CREATE OR REPLACE FUNCTION admin_project_datasets(p_project_id uuid)
RETURNS TABLE(
  id uuid,
  name text,
  published boolean,
  import_status text,
  created_at timestamptz
)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT d.id, d.name, d.published,
         -- Null for a dataset that was never imported from the Hub (0013);
         -- the newest import is the one whose state still means anything.
         (SELECT i.status FROM dataset_imports i
           WHERE i.dataset_id = d.id ORDER BY i.created_at DESC LIMIT 1),
         d.created_at
  FROM datasets d
  JOIN projects p ON p.id = d.project_id AND p.deleted_at IS NULL
  WHERE d.project_id = p_project_id
  ORDER BY d.created_at DESC;
$$;
REVOKE ALL ON FUNCTION admin_project_datasets(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_project_datasets(uuid) TO core_app;

-- Every deployment the project has had, newest first — including the
-- failed and deleted ones, which are the history an operator opens this
-- to read.
CREATE OR REPLACE FUNCTION admin_project_deployments(p_project_id uuid)
RETURNS TABLE(
  id uuid,
  training_run_id uuid,
  route_prefix text,
  -- deploying | active | failed | deleted, as Ray left it.
  status text,
  rate_limit_per_minute int,
  created_at timestamptz
)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT d.id, d.training_run_id, d.route_prefix, d.status, d.rate_limit_per_minute, d.created_at
  FROM deployments d
  JOIN projects p ON p.id = d.project_id AND p.deleted_at IS NULL
  WHERE d.project_id = p_project_id
  ORDER BY d.created_at DESC, d.id DESC;
$$;
REVOKE ALL ON FUNCTION admin_project_deployments(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_project_deployments(uuid) TO core_app;

-- project_members is the platform's own record of who has access; the
-- route joins Zitadel's names onto it.
CREATE OR REPLACE FUNCTION admin_project_members(p_project_id uuid)
RETURNS TABLE(user_id text, role text, joined_at timestamptz)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT m.user_id, m.role, m.created_at
  FROM project_members m
  JOIN projects p ON p.id = m.project_id AND p.deleted_at IS NULL
  WHERE m.project_id = p_project_id
  ORDER BY m.created_at;
$$;
REVOKE ALL ON FUNCTION admin_project_members(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_project_members(uuid) TO core_app;
