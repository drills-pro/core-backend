-- migrate:up
-- Collaboration/visibility (improvement/phase2/08-collaboration-visibility.md).
-- Additive per 11-migration-phasing.md step 5: visibility/publish_* columns
-- default to private/false, existing projects unaffected until an owner
-- opts in.

ALTER TABLE projects ADD COLUMN IF NOT EXISTS visibility text NOT NULL DEFAULT 'private';
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_constraint WHERE conname = 'projects_visibility_check') THEN
    ALTER TABLE projects ADD CONSTRAINT projects_visibility_check
      CHECK (visibility IN ('private', 'public'));
  END IF;
END
$$;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS publish_project_metadata boolean NOT NULL DEFAULT false;

-- FR6: publishing a dataset publishes its current LakeFS commit at publish
-- time, not a live pointer — published_ref is that pinned commit;
-- publishing again overwrites it.
ALTER TABLE datasets ADD COLUMN IF NOT EXISTS published boolean NOT NULL DEFAULT false;
ALTER TABLE datasets ADD COLUMN IF NOT EXISTS published_ref text;

-- FR7: two independent disclosures — metrics/config vs. the artifact itself.
ALTER TABLE model_versions ADD COLUMN IF NOT EXISTS publish_metadata boolean NOT NULL DEFAULT false;
ALTER TABLE model_versions ADD COLUMN IF NOT EXISTS publish_artifact boolean NOT NULL DEFAULT false;

-- FR8: SECURITY DEFINER, tightly scoped to visibility='public' rows only —
-- same bypass pattern as project_exists_any_tenant (0001) and the
-- inflight_*_tenant_ids helpers, never a blanket RLS bypass.
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
REVOKE ALL ON FUNCTION list_public_projects(int, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION list_public_projects(int, int) TO core_app;

-- FR5: datasets within a public project — private ones list as "1 private
-- dataset" with no name leaked, per the literal ask.
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
REVOKE ALL ON FUNCTION list_public_project_datasets(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION list_public_project_datasets(uuid) TO core_app;

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
REVOKE ALL ON FUNCTION list_public_project_models(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION list_public_project_models(uuid) TO core_app;

-- migrate:down
DROP FUNCTION IF EXISTS list_public_project_models(uuid);
DROP FUNCTION IF EXISTS list_public_project_datasets(uuid);
DROP FUNCTION IF EXISTS list_public_projects(int, int);
ALTER TABLE model_versions DROP COLUMN IF EXISTS publish_artifact;
ALTER TABLE model_versions DROP COLUMN IF EXISTS publish_metadata;
ALTER TABLE datasets DROP COLUMN IF EXISTS published_ref;
ALTER TABLE datasets DROP COLUMN IF EXISTS published;
ALTER TABLE projects DROP COLUMN IF EXISTS publish_project_metadata;
ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_visibility_check;
ALTER TABLE projects DROP COLUMN IF EXISTS visibility;
