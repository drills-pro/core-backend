-- migrate:up
-- The curated base models a tenant can fine-tune or deploy from (docs 17, catalog). Platform data, not tenant data: no
-- tenant_id, so no RLS; only /v1/admin routes write it and every tenant reads the enabled rows. `task` is a task slug kept
-- as plain text and checked by the service against task_types, not a foreign key, because task types are becoming data.
-- A model is pinned by commit sha (never a branch) and records the licence it was admitted under.
CREATE TABLE model_catalog (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source text NOT NULL DEFAULT 'hf' CONSTRAINT model_catalog_source_check CHECK (source IN ('hf')),
  repo_id text NOT NULL CHECK (repo_id <> ''),
  revision text NOT NULL CHECK (revision ~ '^[0-9a-f]{40}$'),
  display_name text NOT NULL CHECK (display_name <> ''),
  task text NOT NULL CHECK (task <> ''),
  license text NOT NULL CHECK (license <> ''),
  params bigint CHECK (params > 0),
  size_bytes bigint CHECK (size_bytes >= 0),
  status text NOT NULL DEFAULT 'enabled' CONSTRAINT model_catalog_status_check CHECK (status IN ('enabled', 'disabled')),
  created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_by text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source, repo_id, revision, task)
);
CREATE INDEX model_catalog_task_idx ON model_catalog (task, status);
GRANT SELECT, INSERT, UPDATE, DELETE ON model_catalog TO core_app;

-- The list the platform's presets already pin becomes the first rows, so the catalog is not empty on day one.
INSERT INTO model_catalog (repo_id, revision, display_name, task, license, created_by, updated_by)
SELECT DISTINCT p.base_model_repo, p.base_model_revision, split_part(p.base_model_repo, '/', 2), p.task,
       COALESCE(p.base_model_license, CASE WHEN p.base_model_repo = 'FacebookAI/roberta-base' THEN 'mit' ELSE 'apache-2.0' END),
       'migration', 'migration'
FROM task_presets p;

-- migrate:down
DROP TABLE model_catalog;
