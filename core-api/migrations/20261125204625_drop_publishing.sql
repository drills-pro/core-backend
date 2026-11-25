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
-- Puts back the columns, the generated flags and the constraint as the sharing migration left them. The gallery
-- functions come back from their earlier migrations if needed.
ALTER TABLE model_versions
  ADD COLUMN publish_metadata boolean NOT NULL DEFAULT true,
  ADD COLUMN publish_artifact boolean NOT NULL DEFAULT true;
ALTER TABLE projects
  ADD COLUMN sharing text NOT NULL DEFAULT 'private'
    CONSTRAINT projects_sharing_check CHECK (sharing IN ('private', 'gallery', 'gallery_downloads')),
  ADD COLUMN gallery_published boolean GENERATED ALWAYS AS (sharing <> 'private') STORED,
  ADD COLUMN publish_project_metadata boolean GENERATED ALWAYS AS (sharing <> 'private') STORED,
  ADD CONSTRAINT projects_sharing_internal_check CHECK (sharing = 'private' OR visibility = 'internal');
