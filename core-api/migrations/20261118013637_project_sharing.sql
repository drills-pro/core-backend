-- migrate:up
-- One project-level Sharing choice (planning/27 #4): private | gallery (metadata only) | gallery_downloads
-- (model artifacts and published datasets too). It replaces the gallery_published / publish_project_metadata pair,
-- which stay for one release as columns derived from it, so nothing can drift. The per-model publish_metadata and
-- publish_artifact flags now only exclude one version (default true); a model is shown or downloadable when the
-- project level allows it and its flag is not switched off.
ALTER TABLE projects
  ADD COLUMN sharing text NOT NULL DEFAULT 'private'
    CONSTRAINT projects_sharing_check CHECK (sharing IN ('private', 'gallery', 'gallery_downloads'));

-- Map each existing flag combination. A listing without the owner's opt-in to publish the metadata
-- (gallery_published without publish_project_metadata) never showed the name, so it goes private rather than
-- start showing it. Gallery plus any model artifact or dataset version already published is the downloads level.
UPDATE projects p SET sharing = CASE
  WHEN NOT p.gallery_published OR NOT p.publish_project_metadata THEN 'private'
  WHEN EXISTS (SELECT 1 FROM model_versions m WHERE m.project_id = p.id AND m.publish_artifact)
    OR EXISTS (SELECT 1 FROM datasets d WHERE d.project_id = p.id AND d.deleted_at IS NULL AND d.published_version_id IS NOT NULL)
    THEN 'gallery_downloads'
  ELSE 'gallery' END;

-- Per-model flags turn from "publish this" into "exclude this" (true = not excluded). Keep what a project that is
-- shared today chose; give every other version the new default so raising the level later does not hide what the
-- owner never decided about.
UPDATE model_versions m SET publish_metadata = true
  FROM projects p WHERE p.id = m.project_id AND p.sharing = 'private';
UPDATE model_versions m SET publish_artifact = true
  FROM projects p WHERE p.id = m.project_id AND p.sharing <> 'gallery_downloads';
ALTER TABLE model_versions ALTER COLUMN publish_metadata SET DEFAULT true, ALTER COLUMN publish_artifact SET DEFAULT true;

-- The old flags become derived columns. The gallery CHECK no longer reads gallery_published.
ALTER TABLE projects DROP CONSTRAINT projects_gallery_internal_check;
-- migration-lint: allow-destructive (replaced by the generated columns of the same name, derived from sharing)
ALTER TABLE projects DROP COLUMN gallery_published, DROP COLUMN publish_project_metadata;
ALTER TABLE projects
  ADD COLUMN gallery_published boolean GENERATED ALWAYS AS (sharing <> 'private') STORED,
  ADD COLUMN publish_project_metadata boolean GENERATED ALWAYS AS (sharing <> 'private') STORED,
  ADD CONSTRAINT projects_sharing_internal_check CHECK (sharing = 'private' OR visibility = 'internal');

-- The gallery reads sharing. Metadata is shown at every gallery level; artifact and dataset exposure needs the
-- downloads level.
CREATE OR REPLACE FUNCTION list_public_projects(p_limit int, p_offset int)
RETURNS TABLE(id uuid, name text, task_type text, created_at timestamptz)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT p.id, p.name, p.task_type, p.created_at
  FROM projects p
  WHERE p.sharing <> 'private' AND p.visibility = 'internal' AND p.status = 'active'
  ORDER BY p.created_at DESC
  LIMIT p_limit OFFSET p_offset;
$$;

CREATE OR REPLACE FUNCTION list_public_project_datasets(p_project_id uuid)
RETURNS TABLE(id uuid, name text, published boolean, version_number integer, stats jsonb)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT d.id,
         CASE WHEN v.id IS NOT NULL AND p.sharing = 'gallery_downloads' THEN d.name ELSE NULL END,
         v.id IS NOT NULL AND p.sharing = 'gallery_downloads',
         CASE WHEN p.sharing = 'gallery_downloads' THEN v.number ELSE NULL END,
         CASE WHEN p.sharing = 'gallery_downloads' THEN v.stats ELSE NULL END
  FROM datasets d
  JOIN projects p ON p.id = d.project_id
  LEFT JOIN dataset_versions v ON v.id = d.published_version_id
  WHERE d.project_id = p_project_id AND d.deleted_at IS NULL
    AND p.sharing <> 'private' AND p.visibility = 'internal' AND p.status = 'active';
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

-- migrate:down
-- Back to "publish this": only what a shared project chose is kept; the flags of a project that was never shown
-- go back to false (the up half set them true, and what they held before is gone).
UPDATE model_versions m SET publish_metadata = false
  FROM projects p WHERE p.id = m.project_id AND p.sharing = 'private';
UPDATE model_versions m SET publish_artifact = false
  FROM projects p WHERE p.id = m.project_id AND p.sharing <> 'gallery_downloads';

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

CREATE OR REPLACE FUNCTION list_public_project_datasets(p_project_id uuid)
RETURNS TABLE(id uuid, name text, published boolean, version_number integer, stats jsonb)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT d.id,
         CASE WHEN v.id IS NOT NULL THEN d.name ELSE NULL END,
         v.id IS NOT NULL,
         v.number,
         v.stats
  FROM datasets d
  JOIN projects p ON p.id = d.project_id
  LEFT JOIN dataset_versions v ON v.id = d.published_version_id
  WHERE d.project_id = p_project_id AND d.deleted_at IS NULL
    AND p.gallery_published AND p.visibility = 'internal' AND p.status = 'active';
$$;

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

ALTER TABLE projects DROP CONSTRAINT projects_sharing_internal_check;
ALTER TABLE projects DROP COLUMN gallery_published, DROP COLUMN publish_project_metadata;
ALTER TABLE projects
  ADD COLUMN gallery_published boolean NOT NULL DEFAULT false,
  ADD COLUMN publish_project_metadata boolean NOT NULL DEFAULT false;
UPDATE projects SET gallery_published = true, publish_project_metadata = true WHERE sharing <> 'private';
ALTER TABLE projects ADD CONSTRAINT projects_gallery_internal_check CHECK (NOT gallery_published OR visibility = 'internal');
ALTER TABLE model_versions ALTER COLUMN publish_metadata SET DEFAULT false, ALTER COLUMN publish_artifact SET DEFAULT false;
-- migration-lint: allow-destructive (down of the migration that added it)
ALTER TABLE projects DROP COLUMN sharing;
