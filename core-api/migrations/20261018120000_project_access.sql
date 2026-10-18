-- migrate:up
-- Project access model (planning/04 "Authorization model"): several role bindings per member,
-- internal/private visibility with a default_access bundle, gallery publishing as its own flag.
-- RLS stays tenant-only; who may do what inside a project is decided by the app (core/authz).

-- Tenant suspension is a state of the tenant row, so the guard needs no call to Zitadel.
ALTER TABLE tenants
  ADD COLUMN status text NOT NULL DEFAULT 'active',
  ADD CONSTRAINT tenants_status_check CHECK (status IN ('active', 'suspended'));

-- Bindings: one row per (project, person, role). user_sub is the Zitadel subject.
ALTER TABLE project_members DROP CONSTRAINT project_members_pkey;
ALTER TABLE project_members DROP CONSTRAINT project_members_role_check;
ALTER TABLE project_members RENAME COLUMN user_id TO user_sub;
UPDATE project_members SET role = CASE role WHEN 'project_editor' THEN 'editor' ELSE 'viewer' END;
ALTER TABLE project_members
  ADD CONSTRAINT project_members_role_check
    CHECK (role IN ('viewer', 'editor', 'dataset_editor', 'model_editor', 'deployer', 'project_admin')),
  ADD PRIMARY KEY (project_id, user_sub, role);

ALTER TABLE project_invitations DROP CONSTRAINT project_invitations_role_check;
UPDATE project_invitations SET role = CASE role WHEN 'project_editor' THEN 'editor' ELSE 'viewer' END;
ALTER TABLE project_invitations
  ADD CONSTRAINT project_invitations_role_check
    CHECK (role IN ('viewer', 'editor', 'dataset_editor', 'model_editor', 'deployer', 'project_admin'));

-- Visibility is private | internal; 'public' becomes the gallery flag on an internal project.
ALTER TABLE projects ADD COLUMN gallery_published boolean NOT NULL DEFAULT false;
ALTER TABLE projects DROP CONSTRAINT projects_visibility_check;
UPDATE projects SET gallery_published = true, visibility = 'internal' WHERE visibility = 'public';
ALTER TABLE projects
  ALTER COLUMN visibility SET DEFAULT 'internal',
  ADD COLUMN default_access text NOT NULL DEFAULT 'read',
  ADD CONSTRAINT projects_visibility_check CHECK (visibility IN ('private', 'internal')),
  ADD CONSTRAINT projects_default_access_check CHECK (default_access IN ('read', 'edit')),
  ADD CONSTRAINT projects_gallery_internal_check CHECK (NOT gallery_published OR visibility = 'internal');

-- Functions that read the renamed or re-valued columns.
CREATE OR REPLACE FUNCTION list_public_projects(p_limit int, p_offset int)
RETURNS TABLE(id uuid, name text, task_type text, created_at timestamptz)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT p.id,
         CASE WHEN p.publish_project_metadata THEN p.name ELSE NULL END,
         CASE WHEN p.publish_project_metadata THEN p.task_type ELSE NULL END,
         p.created_at
  FROM projects p
  WHERE p.gallery_published AND p.visibility = 'internal' AND p.status = 'active'
  ORDER BY p.created_at DESC
  LIMIT p_limit OFFSET p_offset;
$$;

CREATE OR REPLACE FUNCTION list_public_project_datasets(p_project_id uuid)
RETURNS TABLE(id uuid, name text, published boolean, published_ref text)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT d.id,
         CASE WHEN d.published THEN d.name ELSE NULL END,
         d.published,
         CASE WHEN d.published THEN d.published_ref ELSE NULL END
  FROM datasets d
  JOIN projects p ON p.id = d.project_id
  WHERE d.project_id = p_project_id AND p.gallery_published AND p.visibility = 'internal' AND p.status = 'active';
$$;

CREATE OR REPLACE FUNCTION list_public_project_models(p_project_id uuid)
RETURNS TABLE(id uuid, mlflow_model_name text, mlflow_version text, publish_metadata boolean, publish_artifact boolean)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT m.id,
         CASE WHEN m.publish_metadata THEN m.mlflow_model_name ELSE NULL END,
         CASE WHEN m.publish_metadata THEN m.mlflow_version ELSE NULL END,
         m.publish_metadata,
         m.publish_artifact
  FROM model_versions m
  JOIN projects p ON p.id = m.project_id
  WHERE m.project_id = p_project_id AND p.gallery_published AND p.visibility = 'internal' AND p.status = 'active';
$$;

CREATE OR REPLACE FUNCTION admin_tenant_stats(p_tenant_ids uuid[])
RETURNS TABLE(tenant_id uuid, project_count bigint, member_count bigint)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT t.id,
         (SELECT count(*) FROM projects p
           WHERE p.tenant_id = t.id AND p.deleted_at IS NULL),
         (SELECT count(DISTINCT m.user_sub) FROM project_members m
           WHERE m.tenant_id = t.id)
  FROM unnest(p_tenant_ids) AS t(id);
$$;

CREATE OR REPLACE FUNCTION admin_platform_stats()
RETURNS TABLE(
  tenant_count bigint,
  tenant_count_30d bigint,
  project_count bigint,
  project_count_30d bigint,
  member_count bigint
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT (SELECT count(*) FROM tenants),
         (SELECT count(*) FROM tenants WHERE created_at >= now() - interval '30 days'),
         (SELECT count(*) FROM projects WHERE deleted_at IS NULL),
         (SELECT count(*) FROM projects
           WHERE deleted_at IS NULL AND created_at >= now() - interval '30 days'),
         (SELECT count(DISTINCT user_sub) FROM project_members);
$$;

CREATE OR REPLACE FUNCTION admin_tenant_members(p_tenant_id uuid)
RETURNS TABLE(
  user_id text,
  roles text[],
  projects text[],
  project_count bigint,
  joined_at timestamptz
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT m.user_sub,
         array_agg(DISTINCT m.role),
         array_agg(DISTINCT p.name),
         count(DISTINCT m.project_id),
         min(m.created_at)
  FROM project_members m
  JOIN projects p ON p.id = m.project_id AND p.deleted_at IS NULL
  WHERE m.tenant_id = p_tenant_id
  GROUP BY m.user_sub
  ORDER BY min(m.created_at);
$$;

-- Authorization reads. Both run as the owner and filter by the caller and tenant they are given.
-- project_access: one row when p_sub belongs to p_tenant (tenant role and status), none otherwise.
-- The project columns are null when p_project is null or not in that tenant, so the app can answer
-- "not a member", "suspended" and "no such project" from the same call.
CREATE FUNCTION project_access(p_sub text, p_tenant uuid, p_project uuid)
RETURNS TABLE(tenant_role text, tenant_status text, visibility text, default_access text, roles text[])
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT m.role, t.status, p.visibility, p.default_access,
         CASE WHEN p.id IS NULL THEN NULL ELSE COALESCE(
           (SELECT array_agg(pm.role) FROM project_members pm
             WHERE pm.project_id = p.id AND pm.user_sub = p_sub), '{}') END
  FROM tenant_members m
  JOIN tenants t ON t.id = m.tenant_id
  LEFT JOIN projects p ON p.id = p_project AND p.tenant_id = m.tenant_id
  WHERE m.user_sub = p_sub AND m.tenant_id = p_tenant;
$$;

-- accessible_project_ids: the projects p_sub may see in p_tenant, for filtering list endpoints.
CREATE FUNCTION accessible_project_ids(p_sub text, p_tenant uuid) RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT p.id
  FROM projects p
  JOIN tenant_members m ON m.tenant_id = p.tenant_id AND m.user_sub = p_sub
  WHERE p.tenant_id = p_tenant
    AND (m.role IN ('owner', 'admin')
         OR p.visibility = 'internal'
         OR EXISTS (SELECT 1 FROM project_members pm WHERE pm.project_id = p.id AND pm.user_sub = p_sub));
$$;

REVOKE ALL ON FUNCTION project_access(text, uuid, uuid), accessible_project_ids(text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION project_access(text, uuid, uuid), accessible_project_ids(text, uuid) TO core_app;

-- Cross-tenant 404 is decided by project_access; the existence probe has no caller left.
DROP FUNCTION project_exists_any_tenant(uuid);

-- migrate:down
CREATE FUNCTION project_exists_any_tenant(p_id uuid) RETURNS boolean
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT EXISTS(SELECT 1 FROM projects WHERE id = p_id);
$$;
REVOKE ALL ON FUNCTION project_exists_any_tenant(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION project_exists_any_tenant(uuid) TO core_app;

DROP FUNCTION project_access(text, uuid, uuid), accessible_project_ids(text, uuid);

-- Back to one role per person: any write role becomes project_editor, else project_viewer.
ALTER TABLE project_members DROP CONSTRAINT project_members_pkey;
ALTER TABLE project_members DROP CONSTRAINT project_members_role_check;
UPDATE project_members SET role = CASE role WHEN 'viewer' THEN 'project_viewer' ELSE 'project_editor' END;
DELETE FROM project_members a USING project_members b
  WHERE a.project_id = b.project_id AND a.user_sub = b.user_sub
    AND (a.role, a.ctid) > (b.role, b.ctid);
ALTER TABLE project_members RENAME COLUMN user_sub TO user_id;
ALTER TABLE project_members
  ADD CONSTRAINT project_members_role_check CHECK (role IN ('project_editor', 'project_viewer')),
  ADD PRIMARY KEY (project_id, user_id);

ALTER TABLE project_invitations DROP CONSTRAINT project_invitations_role_check;
UPDATE project_invitations SET role = CASE role WHEN 'viewer' THEN 'project_viewer' ELSE 'project_editor' END;
ALTER TABLE project_invitations
  ADD CONSTRAINT project_invitations_role_check CHECK (role IN ('project_editor', 'project_viewer'));

ALTER TABLE projects DROP CONSTRAINT projects_gallery_internal_check, DROP CONSTRAINT projects_visibility_check;
UPDATE projects SET visibility = CASE WHEN gallery_published THEN 'public' ELSE 'private' END;
ALTER TABLE projects
  ALTER COLUMN visibility SET DEFAULT 'private',
  ADD CONSTRAINT projects_visibility_check CHECK (visibility IN ('private', 'public'));
-- migration-lint: allow-destructive (down of the migration that added them)
ALTER TABLE projects DROP COLUMN default_access, DROP COLUMN gallery_published;

-- migration-lint: allow-destructive (down of the migration that added it)
ALTER TABLE tenants DROP COLUMN status;

CREATE OR REPLACE FUNCTION list_public_projects(p_limit int, p_offset int)
RETURNS TABLE(id uuid, name text, task_type text, created_at timestamptz)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT p.id,
         CASE WHEN p.publish_project_metadata THEN p.name ELSE NULL END,
         CASE WHEN p.publish_project_metadata THEN p.task_type ELSE NULL END,
         p.created_at
  FROM projects p
  WHERE p.visibility = 'public' AND p.status = 'active'
  ORDER BY p.created_at DESC
  LIMIT p_limit OFFSET p_offset;
$$;

CREATE OR REPLACE FUNCTION list_public_project_datasets(p_project_id uuid)
RETURNS TABLE(id uuid, name text, published boolean, published_ref text)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT d.id,
         CASE WHEN d.published THEN d.name ELSE NULL END,
         d.published,
         CASE WHEN d.published THEN d.published_ref ELSE NULL END
  FROM datasets d
  JOIN projects p ON p.id = d.project_id
  WHERE d.project_id = p_project_id AND p.visibility = 'public' AND p.status = 'active';
$$;

CREATE OR REPLACE FUNCTION list_public_project_models(p_project_id uuid)
RETURNS TABLE(id uuid, mlflow_model_name text, mlflow_version text, publish_metadata boolean, publish_artifact boolean)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT m.id,
         CASE WHEN m.publish_metadata THEN m.mlflow_model_name ELSE NULL END,
         CASE WHEN m.publish_metadata THEN m.mlflow_version ELSE NULL END,
         m.publish_metadata,
         m.publish_artifact
  FROM model_versions m
  JOIN projects p ON p.id = m.project_id
  WHERE m.project_id = p_project_id AND p.visibility = 'public' AND p.status = 'active';
$$;

CREATE OR REPLACE FUNCTION admin_tenant_stats(p_tenant_ids uuid[])
RETURNS TABLE(tenant_id uuid, project_count bigint, member_count bigint)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT t.id,
         (SELECT count(*) FROM projects p
           WHERE p.tenant_id = t.id AND p.deleted_at IS NULL),
         (SELECT count(DISTINCT m.user_id) FROM project_members m
           WHERE m.tenant_id = t.id)
  FROM unnest(p_tenant_ids) AS t(id);
$$;

CREATE OR REPLACE FUNCTION admin_platform_stats()
RETURNS TABLE(
  tenant_count bigint,
  tenant_count_30d bigint,
  project_count bigint,
  project_count_30d bigint,
  member_count bigint
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT (SELECT count(*) FROM tenants),
         (SELECT count(*) FROM tenants WHERE created_at >= now() - interval '30 days'),
         (SELECT count(*) FROM projects WHERE deleted_at IS NULL),
         (SELECT count(*) FROM projects
           WHERE deleted_at IS NULL AND created_at >= now() - interval '30 days'),
         (SELECT count(DISTINCT user_id) FROM project_members);
$$;

CREATE OR REPLACE FUNCTION admin_tenant_members(p_tenant_id uuid)
RETURNS TABLE(
  user_id text,
  roles text[],
  projects text[],
  project_count bigint,
  joined_at timestamptz
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT m.user_id,
         array_agg(DISTINCT m.role),
         array_agg(DISTINCT p.name),
         count(DISTINCT m.project_id),
         min(m.created_at)
  FROM project_members m
  JOIN projects p ON p.id = m.project_id AND p.deleted_at IS NULL
  WHERE m.tenant_id = p_tenant_id
  GROUP BY m.user_id
  ORDER BY min(m.created_at);
$$;
