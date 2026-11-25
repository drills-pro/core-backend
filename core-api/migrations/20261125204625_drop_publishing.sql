-- migrate:up
-- No publishing any more: the public gallery, project Sharing, the per-model publish switches and the public
-- dataset listing are cut. Visibility stays at private and internal, so a project is only ever seen by tenant members.

DROP FUNCTION list_public_projects(int, int);
DROP FUNCTION list_public_project_models(uuid);

ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_sharing_internal_check;
ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_gallery_internal_check;

-- migration-lint: allow-destructive (cut for now - generated from sharing, nothing reads them once the gallery is gone)
ALTER TABLE projects DROP COLUMN gallery_published;
-- migration-lint: allow-destructive (cut for now - generated from sharing, nothing reads them once the gallery is gone)
ALTER TABLE projects DROP COLUMN publish_project_metadata;
-- migration-lint: allow-destructive (cut for now - the sharing level is removed, nothing reads it)
ALTER TABLE projects DROP COLUMN sharing;
-- migration-lint: allow-destructive (cut for now - per-model publish switches are removed, nothing reads them)
ALTER TABLE model_versions DROP COLUMN publish_metadata;
-- migration-lint: allow-destructive (cut for now - per-model publish switches are removed, nothing reads them)
ALTER TABLE model_versions DROP COLUMN publish_artifact;

-- migrate:down
-- Restores the columns and constraint as the sharing migration left them, then the gallery functions as they stood.
ALTER TABLE model_versions
  ADD COLUMN publish_metadata boolean NOT NULL DEFAULT true,
  ADD COLUMN publish_artifact boolean NOT NULL DEFAULT true;
ALTER TABLE projects
  ADD COLUMN sharing text NOT NULL DEFAULT 'private'
    CONSTRAINT projects_sharing_check CHECK (sharing IN ('private', 'gallery', 'gallery_downloads')),
  ADD COLUMN gallery_published boolean GENERATED ALWAYS AS (sharing <> 'private') STORED,
  ADD COLUMN publish_project_metadata boolean GENERATED ALWAYS AS (sharing <> 'private') STORED,
  ADD CONSTRAINT projects_sharing_internal_check CHECK (sharing = 'private' OR visibility = 'internal');

CREATE OR REPLACE FUNCTION list_public_projects(p_limit int, p_offset int)
RETURNS TABLE(id uuid, name text, task_type text, created_at timestamptz)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT p.id, p.name, p.task_type, p.created_at
  FROM projects p
  WHERE p.sharing <> 'private' AND p.visibility = 'internal' AND p.status = 'active'
  ORDER BY p.created_at DESC
  LIMIT p_limit OFFSET p_offset;
$$;


CREATE OR REPLACE FUNCTION list_public_project_models(p_project_id uuid)
RETURNS TABLE(id uuid, mlflow_model_name text, mlflow_version text, publish_metadata boolean, publish_artifact boolean)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT m.id,
         CASE WHEN m.publish_metadata THEN m.mlflow_model_name ELSE NULL END,
         CASE WHEN m.publish_metadata THEN m.mlflow_version ELSE NULL END,
         m.publish_metadata,
         m.publish_artifact AND p.sharing = 'gallery_downloads'
  FROM model_versions m
  JOIN projects p ON p.id = m.project_id
  WHERE m.project_id = p_project_id AND p.sharing <> 'private' AND p.visibility = 'internal' AND p.status = 'active';
$$;


