-- One tenant as the back-office sees it (app/modules/admin): the detail
-- page behind a row of the /v1/admin/tenants listing.
--
-- Two things live here. First, org-level settings: the listing's three
-- policy switches have nowhere to live, since `tenants` held nothing but
-- the Zitadel mapping and the plan tier. Same lean jsonb as
-- projects.settings (0019) — nothing in here needs its own index, and the
-- API owns the key vocabulary.
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS settings jsonb NOT NULL DEFAULT '{}'::jsonb;
-- 0001 granted SELECT only. The back-office is the one writer, and it
-- writes nothing else on this table: name and zitadel_org_id belong to
-- provisioning, plan_tier to billing.
GRANT UPDATE (settings) ON tenants TO core_app;

-- Second, the reads. projects, datasets, model_versions and
-- project_members are all RLS-protected and the admin connection sets no
-- `app.tenant_id`, so these are the same SECURITY DEFINER escape as
-- admin_tenant_stats (0025), each one pinned to the single tenant the
-- caller asked for.

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
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT p.id, p.name, p.status, p.task_type, p.visibility, p.created_at,
         (SELECT count(*) FROM datasets d WHERE d.project_id = p.id),
         (SELECT count(*) FROM model_versions m WHERE m.project_id = p.id),
         -- The newest registered version is what the card names; the rest
         -- are only a count.
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
REVOKE ALL ON FUNCTION admin_tenant_projects(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_tenant_projects(uuid) TO core_app;

-- One row per person, not per membership: someone editing two projects in
-- this tenant is one member holding two project rows. Matches how
-- admin_tenant_stats counts them (0025), so the card and this list agree.
CREATE OR REPLACE FUNCTION admin_tenant_members(p_tenant_id uuid)
RETURNS TABLE(user_id text, roles text[], project_count bigint, joined_at timestamptz)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT m.user_id,
         array_agg(DISTINCT m.role),
         count(DISTINCT m.project_id),
         min(m.created_at)
  FROM project_members m
  JOIN projects p ON p.id = m.project_id AND p.deleted_at IS NULL
  WHERE m.tenant_id = p_tenant_id
  GROUP BY m.user_id
  ORDER BY min(m.created_at);
$$;
REVOKE ALL ON FUNCTION admin_tenant_members(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_tenant_members(uuid) TO core_app;

CREATE OR REPLACE FUNCTION admin_tenant_datasets(p_tenant_id uuid)
RETURNS TABLE(
  id uuid,
  name text,
  project_id uuid,
  project_name text,
  import_status text,
  created_at timestamptz
)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT d.id, d.name, p.id, p.name,
         -- Null for a dataset that was never imported from the Hub (0013);
         -- the newest import is the one whose state still means anything.
         (SELECT i.status FROM dataset_imports i
           WHERE i.dataset_id = d.id ORDER BY i.created_at DESC LIMIT 1),
         d.created_at
  FROM datasets d
  JOIN projects p ON p.id = d.project_id
  WHERE d.tenant_id = p_tenant_id AND p.deleted_at IS NULL
  ORDER BY d.created_at DESC;
$$;
REVOKE ALL ON FUNCTION admin_tenant_datasets(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_tenant_datasets(uuid) TO core_app;

-- The four numbers in the detail header. projects/members repeat
-- admin_tenant_stats so the page asks once instead of twice.
CREATE OR REPLACE FUNCTION admin_tenant_detail_stats(p_tenant_id uuid)
RETURNS TABLE(
  project_count bigint,
  member_count bigint,
  dataset_count bigint,
  model_count bigint
)
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT s.project_count, s.member_count,
         (SELECT count(*) FROM datasets d WHERE d.tenant_id = p_tenant_id),
         (SELECT count(*) FROM model_versions m WHERE m.tenant_id = p_tenant_id)
  FROM admin_tenant_stats(ARRAY[p_tenant_id]) s;
$$;
REVOKE ALL ON FUNCTION admin_tenant_detail_stats(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_tenant_detail_stats(uuid) TO core_app;
