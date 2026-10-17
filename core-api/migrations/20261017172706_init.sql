-- migrate:up
-- core-api's schema from scratch (it replaces the hand-applied 0001-0035 files; prod was empty).
--
-- Applied by dbmate as backend_admin: the role k8s already provisions for this app (CNPG managed
-- role, owner of the database, Vault static creds `backend-admin`). It is not a superuser and has
-- no BYPASSRLS, and this file creates no roles: core_app is created by the cluster (locally by
-- compose/postgres/core-api.sql).
--
-- backend_admin owns every object; core_app (the NOLOGIN group of the runtime's Vault users) owns
-- nothing. There are no default privileges, so everything core_app uses is granted below.
--
-- RLS is ENABLEd, not FORCEd. core_app is not the owner, so the policies always apply to it. The
-- owner skips them, and that is what the SECURITY DEFINER functions rely on to read across tenants:
-- each runs as backend_admin, pins search_path and filters rows itself.

DO $$
BEGIN
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO core_app', current_database());
END
$$;
-- USAGE only: runtime users never create objects (they would own them, and Vault could not drop
-- them at lease end).
GRANT USAGE ON SCHEMA public TO core_app;

-- Tenancy ------------------------------------------------------------------------------------

CREATE TABLE tenants (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  -- Zitadel organization ID (req/02-auth-identity.md FR2: org maps 1:1 to tenant).
  zitadel_org_id text NOT NULL UNIQUE,
  name           text NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  -- req/04 FR4: sets a new deployment's default rate limit.
  plan_tier      text NOT NULL DEFAULT 'free' CHECK (plan_tier IN ('free', 'pro', 'enterprise')),
  -- Back-office policy switches; the API owns the key vocabulary.
  settings       jsonb NOT NULL DEFAULT '{}'::jsonb
);

-- Org/team/project model (improvement/phase2/02): every project belongs to one team.
CREATE TABLE teams (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id),
  name        text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX teams_tenant_id_idx ON teams(tenant_id);

CREATE TABLE projects (
  id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id                uuid NOT NULL REFERENCES tenants(id),
  name                     text NOT NULL,
  created_at               timestamptz NOT NULL DEFAULT now(),
  status                   text NOT NULL DEFAULT 'active'
                             CHECK (status IN ('active', 'archived', 'pending_delete')),
  deleted_at               timestamptz,
  team_id                  uuid REFERENCES teams(id),
  -- Set at creation, immutable after the first dataset attach (enforced in app code).
  -- legacy_untyped is the escape hatch for pre-Phase-2 projects.
  task_type                text CHECK (task_type IN ('image_classification', 'object_detection',
                             'tabular_classification', 'text_classification', 'legacy_untyped')),
  -- Pre-fills every training run's test-set picker; FK added once datasets exists.
  default_test_dataset_id  uuid,
  -- compute_target / base_model / deploy_strategy: nothing in here needs its own index.
  settings                 jsonb NOT NULL DEFAULT '{}'::jsonb,
  visibility               text NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'public')),
  publish_project_metadata boolean NOT NULL DEFAULT false
);
CREATE INDEX projects_tenant_id_idx ON projects(tenant_id);
CREATE INDEX projects_team_id_idx ON projects(team_id);

-- Per-project role, independent of org-level role. user_id is the Zitadel subject: Zitadel is the
-- source of truth for identity, there is no local users table.
CREATE TABLE project_members (
  tenant_id   uuid NOT NULL REFERENCES tenants(id),
  project_id  uuid NOT NULL REFERENCES projects(id),
  user_id     text NOT NULL,
  role        text NOT NULL CHECK (role IN ('project_editor', 'project_viewer')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, user_id)
);
CREATE INDEX project_members_tenant_idx ON project_members(tenant_id);

-- Invite by email, 7-day expiry. Only sha256(token) is stored; the plaintext is returned once at
-- creation. The address is cleared once the invite is accepted.
CREATE TABLE project_invitations (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id),
  project_id  uuid NOT NULL REFERENCES projects(id),
  email       text,
  role        text NOT NULL CHECK (role IN ('project_editor', 'project_viewer')),
  invited_by  text NOT NULL,
  expires_at  timestamptz NOT NULL,
  accepted_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now(),
  token_hash  text NOT NULL
);
CREATE INDEX project_invitations_tenant_idx ON project_invitations(tenant_id);
CREATE UNIQUE INDEX project_invitations_token_hash_key ON project_invitations(token_hash);

-- Project-level audit log. Append-only: no UPDATE/DELETE grant.
CREATE TABLE audit_log (
  id          bigserial PRIMARY KEY,
  tenant_id   uuid NOT NULL REFERENCES tenants(id),
  project_id  uuid NOT NULL REFERENCES projects(id),
  actor       text NOT NULL,
  action      text NOT NULL,
  entity_type text NOT NULL,
  entity_id   text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_tenant_project_idx ON audit_log(tenant_id, project_id, created_at DESC);
-- The back-office activity log orders by time across every tenant.
CREATE INDEX audit_log_created_at_idx ON audit_log(created_at DESC, id DESC);

-- Usage metering (req/09). Append-only ledger; UNIQUE(resource_type, source_ref) is the idempotency
-- key, so a retried callback no-ops instead of double-billing. Corrections are offsetting rows.
CREATE TABLE usage_events (
  id              bigserial PRIMARY KEY,
  tenant_id       uuid NOT NULL REFERENCES tenants(id),
  project_id      uuid NOT NULL REFERENCES projects(id),
  resource_type   text NOT NULL CHECK (resource_type IN ('training', 'storage_dataset', 'storage_model', 'inference')),
  quantity        numeric NOT NULL CHECK (quantity >= 0),
  unit            text NOT NULL,
  occurred_at     timestamptz NOT NULL,
  source_ref      text NOT NULL,
  pricing_version text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  team_id         uuid REFERENCES teams(id),
  UNIQUE (resource_type, source_ref)
);
CREATE INDEX usage_events_tenant_idx ON usage_events(tenant_id, occurred_at DESC);
CREATE INDEX usage_events_project_idx ON usage_events(project_id, occurred_at DESC);
CREATE INDEX usage_events_team_id_idx ON usage_events(team_id, occurred_at DESC);

-- Datasets and ML ----------------------------------------------------------------------------

-- published_ref pins the LakeFS commit at publish time, not a live pointer.
CREATE TABLE datasets (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  project_id    uuid NOT NULL REFERENCES projects(id),
  name          text NOT NULL,
  s3_prefix     text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  published     boolean NOT NULL DEFAULT false,
  published_ref text
);
CREATE INDEX datasets_project_idx ON datasets(project_id, created_at DESC);
ALTER TABLE projects ADD FOREIGN KEY (default_test_dataset_id) REFERENCES datasets(id);

CREATE TABLE training_runs (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id             uuid NOT NULL REFERENCES tenants(id),
  project_id            uuid NOT NULL REFERENCES projects(id),
  idempotency_key       text NOT NULL,
  ray_job_id            text NOT NULL,
  mlflow_run_id         text,
  dataset_ref           text NOT NULL,
  status                text NOT NULL DEFAULT 'submitted'
                          CHECK (status IN ('submitted', 'running', 'succeeded', 'failed', 'cancelled')),
  created_at            timestamptz NOT NULL DEFAULT now(),
  completed_at          timestamptz,
  -- The platform-generated Ludwig config, for reproducibility and "export config".
  config                jsonb NOT NULL DEFAULT '{}'::jsonb,
  model_artifact        jsonb,
  metrics               jsonb,
  -- The LakeFS commit trained on; NULL means the branch head at submission.
  dataset_version       text,
  test_dataset_id       uuid REFERENCES datasets(id),
  test_dataset_version  text,
  -- Runs reproduced from the same source share this.
  reproduction_group_id uuid,
  dataset_id            uuid REFERENCES datasets(id),
  -- A client retry with the same key returns the existing run instead of submitting twice.
  UNIQUE (project_id, idempotency_key)
);
CREATE INDEX training_runs_tenant_idx ON training_runs(tenant_id, created_at DESC);
CREATE INDEX training_runs_project_idx ON training_runs(project_id, created_at DESC);
-- Hard-quota check: in-flight runs per tenant.
CREATE INDEX training_runs_inflight_idx ON training_runs(tenant_id) WHERE status IN ('submitted', 'running');
CREATE INDEX training_runs_reproduction_group_idx ON training_runs(reproduction_group_id);
CREATE INDEX training_runs_dataset_id_idx ON training_runs(dataset_id);

CREATE TABLE deployments (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id             uuid NOT NULL REFERENCES tenants(id),
  project_id            uuid NOT NULL REFERENCES projects(id),
  training_run_id       uuid NOT NULL REFERENCES training_runs(id),
  ray_job_id            text NOT NULL,
  route_prefix          text NOT NULL,
  status                text NOT NULL DEFAULT 'deploying'
                          CHECK (status IN ('deploying', 'active', 'failed', 'deleted')),
  created_at            timestamptz NOT NULL DEFAULT now(),
  -- /predict's own credential: a per-deployment secret, not the tenant JWT.
  predict_key_hash      text,
  rate_limit_per_minute integer NOT NULL DEFAULT 60
);
CREATE INDEX deployments_project_idx ON deployments(project_id, created_at DESC);
CREATE INDEX deployments_training_run_idx ON deployments(training_run_id, created_at DESC);

-- The tenant-scoped index onto MLflow's registry, which has no tenant concept.
CREATE TABLE model_versions (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id),
  project_id        uuid NOT NULL REFERENCES projects(id),
  training_run_id   uuid NOT NULL REFERENCES training_runs(id),
  mlflow_model_name text NOT NULL,
  mlflow_version    text NOT NULL,
  stage             text NOT NULL DEFAULT 'None',
  created_at        timestamptz NOT NULL DEFAULT now(),
  -- Two independent disclosures: metrics/config vs. the artifact itself.
  publish_metadata  boolean NOT NULL DEFAULT false,
  publish_artifact  boolean NOT NULL DEFAULT false
);
CREATE INDEX model_versions_project_idx ON model_versions(project_id, created_at DESC);
CREATE INDEX model_versions_created_at_idx ON model_versions(created_at DESC, id DESC);
CREATE INDEX model_versions_model_idx ON model_versions(project_id, mlflow_model_name, created_at DESC);

-- One Label Studio project per platform dataset.
CREATE TABLE labeling_projects (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id),
  project_id      uuid NOT NULL REFERENCES projects(id),
  dataset_id      uuid NOT NULL REFERENCES datasets(id),
  label_studio_id integer NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX labeling_projects_project_idx ON labeling_projects(project_id, created_at DESC);

-- Ray jobs that import a Hugging Face Hub dataset into a platform dataset's prefix.
CREATE TABLE dataset_imports (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL REFERENCES tenants(id),
  project_id   uuid NOT NULL REFERENCES projects(id),
  dataset_id   uuid NOT NULL REFERENCES datasets(id),
  ray_job_id   text NOT NULL,
  hf_dataset   text NOT NULL,
  kind         text NOT NULL CHECK (kind IN ('images', 'tabular')),
  status       text NOT NULL DEFAULT 'submitted' CHECK (status IN ('submitted', 'running', 'succeeded', 'failed')),
  error        text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);
CREATE INDEX dataset_imports_dataset_idx ON dataset_imports(dataset_id, created_at DESC);
CREATE INDEX dataset_imports_inflight_idx ON dataset_imports(tenant_id) WHERE status IN ('submitted', 'running');

-- Last-N test invocations for the in-app "try it" panel.
CREATE TABLE deployment_test_calls (
  id            bigserial PRIMARY KEY,
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  deployment_id uuid NOT NULL REFERENCES deployments(id),
  request       jsonb NOT NULL,
  response      jsonb,
  latency_ms    numeric,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX deployment_test_calls_deployment_idx ON deployment_test_calls(deployment_id, created_at DESC);

-- Preprocessing Ray jobs whose output is a new LakeFS commit.
CREATE TABLE dataset_preprocessing_jobs (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL REFERENCES tenants(id),
  project_id   uuid NOT NULL REFERENCES projects(id),
  dataset_id   uuid NOT NULL REFERENCES datasets(id),
  ray_job_id   text NOT NULL,
  config       jsonb NOT NULL,
  status       text NOT NULL DEFAULT 'submitted' CHECK (status IN ('submitted', 'running', 'succeeded', 'failed')),
  output_ref   text,
  error        text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);
CREATE INDEX dataset_preprocessing_jobs_dataset_idx ON dataset_preprocessing_jobs(dataset_id, created_at DESC);

-- Not tenant-scoped ----------------------------------------------------------------------------

-- Self-serve signup saga (docs/signup-saga.md), keyed by the client's Idempotency-Key. It exists
-- before the tenant does, so no RLS. Key reuse is checked against sha256(lower(email)); the
-- plaintext email is cleared once the signup is done.
CREATE TABLE signups (
  key        text PRIMARY KEY,
  email      text,
  org_name   text NOT NULL,
  state      text NOT NULL DEFAULT 'started' CHECK (state IN ('started', 'org', 'granted', 'done')),
  org_id     text,
  user_id    text,
  tenant_id  uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  email_hash text NOT NULL
);
CREATE INDEX signups_stale_idx ON signups(created_at) WHERE state <> 'done';

-- Shared fixed-window rate-limit counters (app/core/rate_limit.py), so a limit holds across
-- replicas. UNLOGGED: a crash just resets everyone's window. Not "window": a reserved word.
CREATE UNLOGGED TABLE rate_limits (
  key    text    NOT NULL,
  minute bigint  NOT NULL,  -- unix minute
  count  integer NOT NULL,
  PRIMARY KEY (key, minute)
);

-- Row-level security -------------------------------------------------------------------------

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['projects', 'teams', 'project_members', 'project_invitations', 'audit_log',
    'usage_events', 'datasets', 'training_runs', 'deployments', 'model_versions', 'labeling_projects',
    'dataset_imports', 'deployment_test_calls', 'dataset_preprocessing_jobs']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_setting(''app.tenant_id'', true)::uuid)', t);
  END LOOP;
END
$$;

-- Grants to core_app ---------------------------------------------------------------------------

-- tenants: rows are written by provisioning/signup; the back-office edits settings only.
GRANT SELECT, INSERT ON tenants TO core_app;
GRANT UPDATE (settings) ON tenants TO core_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON teams, projects, project_members, project_invitations,
  signups, rate_limits TO core_app;
GRANT SELECT, INSERT, UPDATE ON training_runs, datasets, deployments, model_versions, dataset_imports,
  dataset_preprocessing_jobs TO core_app;
-- Append-only.
GRANT SELECT, INSERT ON audit_log, usage_events, labeling_projects, deployment_test_calls TO core_app;
GRANT USAGE ON SEQUENCE audit_log_id_seq, usage_events_id_seq, deployment_test_calls_id_seq TO core_app;

-- SECURITY DEFINER functions -------------------------------------------------------------------
-- Each runs as backend_admin, skipping RLS, so each filters rows itself and returns no more than
-- its caller may see. Admin ones are reached only from routes behind get_platform_admin.

-- 403 vs 404 (req/01 FR1): a boolean, never another tenant's row.
CREATE FUNCTION project_exists_any_tenant(p_id uuid) RETURNS boolean
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT EXISTS(SELECT 1 FROM projects WHERE id = p_id);
$$;

-- /predict proves possession of the per-deployment secret instead of holding a tenant context.
CREATE FUNCTION deployment_for_predict(p_id uuid, p_key_hash text)
RETURNS TABLE(route_prefix text, status text, rate_limit_per_minute integer, tenant_id uuid, project_id uuid)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT route_prefix, status, rate_limit_per_minute, tenant_id, project_id FROM deployments
  WHERE id = p_id AND predict_key_hash = p_key_hash;
$$;

-- Background reconcilers find tenants with in-flight work before setting app.tenant_id.
CREATE FUNCTION inflight_training_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT DISTINCT tenant_id FROM training_runs WHERE status IN ('submitted', 'running');
$$;

CREATE FUNCTION inflight_import_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT DISTINCT tenant_id FROM dataset_imports WHERE status IN ('submitted', 'running');
$$;

CREATE FUNCTION inflight_deploy_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT DISTINCT tenant_id FROM deployments WHERE status = 'deploying';
$$;

CREATE FUNCTION inflight_preprocessing_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT DISTINCT tenant_id FROM dataset_preprocessing_jobs WHERE status IN ('submitted', 'running');
$$;

-- Public projects (improvement/phase2/08 FR8): visibility='public' rows only, unpublished names
-- masked.
CREATE FUNCTION list_public_projects(p_limit int, p_offset int)
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

-- Private datasets list as "1 private dataset", no name leaked.
CREATE FUNCTION list_public_project_datasets(p_project_id uuid)
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

CREATE FUNCTION list_public_project_models(p_project_id uuid)
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

-- Back-office tenant list counters, for the tenant ids the caller already holds. Members are
-- distinct project members: an org member on no project is not counted.
CREATE FUNCTION admin_tenant_stats(p_tenant_ids uuid[])
RETURNS TABLE(tenant_id uuid, project_count bigint, member_count bigint)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT t.id,
         (SELECT count(*) FROM projects p
           WHERE p.tenant_id = t.id AND p.deleted_at IS NULL),
         (SELECT count(DISTINCT m.user_id) FROM project_members m
           WHERE m.tenant_id = t.id)
  FROM unnest(p_tenant_ids) AS t(id);
$$;

-- Platform-wide stat cards; aggregates only. Members are distinct people across tenants, so this
-- can be smaller than the sum of the per-tenant counts.
CREATE FUNCTION admin_platform_stats()
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

-- One tenant's detail page, each pinned to the tenant the caller asked for.
CREATE FUNCTION admin_tenant_projects(p_tenant_id uuid)
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
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT p.id, p.name, p.status, p.task_type, p.visibility, p.created_at,
         (SELECT count(*) FROM datasets d WHERE d.project_id = p.id),
         (SELECT count(*) FROM model_versions m WHERE m.project_id = p.id),
         -- The newest registered version is what the card names.
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

-- One row per person, with the projects they are on (names sorted by array_agg DISTINCT).
CREATE FUNCTION admin_tenant_members(p_tenant_id uuid)
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
  -- A membership of a soft-deleted project is not access anyone has.
  JOIN projects p ON p.id = m.project_id AND p.deleted_at IS NULL
  WHERE m.tenant_id = p_tenant_id
  GROUP BY m.user_id
  ORDER BY min(m.created_at);
$$;

CREATE FUNCTION admin_tenant_datasets(p_tenant_id uuid)
RETURNS TABLE(
  id uuid,
  name text,
  project_id uuid,
  project_name text,
  import_status text,
  created_at timestamptz
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT d.id, d.name, p.id, p.name,
         -- Null for a dataset never imported from the Hub; the newest import is the one that counts.
         (SELECT i.status FROM dataset_imports i
           WHERE i.dataset_id = d.id ORDER BY i.created_at DESC LIMIT 1),
         d.created_at
  FROM datasets d
  JOIN projects p ON p.id = d.project_id
  WHERE d.tenant_id = p_tenant_id AND p.deleted_at IS NULL
  ORDER BY d.created_at DESC;
$$;

CREATE FUNCTION admin_tenant_detail_stats(p_tenant_id uuid)
RETURNS TABLE(
  project_count bigint,
  member_count bigint,
  dataset_count bigint,
  model_count bigint
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT s.project_count, s.member_count,
         (SELECT count(*) FROM datasets d WHERE d.tenant_id = p_tenant_id),
         (SELECT count(*) FROM model_versions m WHERE m.tenant_id = p_tenant_id)
  FROM admin_tenant_stats(ARRAY[p_tenant_id]) s;
$$;

-- Cross-tenant audit trail for the back-office activity log. It returns rows: the operator is the
-- one audience allowed to see tenants side by side. `total` is the filtered size on every row.
CREATE FUNCTION admin_activity(
  p_limit int,
  p_offset int,
  p_tenant_id uuid,
  p_entity_type text,
  p_q text,
  p_since timestamptz
)
RETURNS TABLE(
  id bigint,
  tenant_id uuid,
  tenant_name text,
  project_id uuid,
  project_name text,
  actor text,
  action text,
  entity_type text,
  entity_id text,
  created_at timestamptz,
  total bigint
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH matching AS (
    SELECT a.id, a.tenant_id, t.name AS tenant_name,
           a.project_id, p.name AS project_name,
           a.actor, a.action, a.entity_type, a.entity_id, a.created_at
    FROM audit_log a
    JOIN tenants t ON t.id = a.tenant_id
    -- LEFT: a project that went away should not take its history with it.
    LEFT JOIN projects p ON p.id = a.project_id
    WHERE (p_tenant_id IS NULL OR a.tenant_id = p_tenant_id)
      AND (p_entity_type IS NULL OR a.entity_type = p_entity_type)
      AND (p_since IS NULL OR a.created_at >= p_since)
      AND (p_q IS NULL
           OR a.actor ILIKE '%' || p_q || '%'
           OR a.action ILIKE '%' || p_q || '%'
           OR a.entity_type ILIKE '%' || p_q || '%'
           OR a.entity_id ILIKE '%' || p_q || '%'
           OR t.name ILIKE '%' || p_q || '%'
           OR p.name ILIKE '%' || p_q || '%')
  )
  SELECT m.*, count(*) OVER ()
  FROM matching m
  ORDER BY m.created_at DESC, m.id DESC
  LIMIT p_limit OFFSET p_offset;
$$;

-- "Today" is the database's day: the same clock that stamped created_at.
CREATE FUNCTION admin_activity_stats()
RETURNS TABLE(events_today bigint, tenants_today bigint)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT count(*) FILTER (WHERE created_at >= date_trunc('day', now())),
         count(DISTINCT tenant_id) FILTER (WHERE created_at >= date_trunc('day', now()))
  FROM audit_log;
$$;

-- Cross-tenant model listing. A model is (project_id, mlflow_model_name) shown as its newest
-- version; status is the newest deployment of the run that version came from:
-- deployed | deploying | stopped | not_deployed.
CREATE FUNCTION admin_models(
  p_limit int,
  p_offset int,
  p_tenant_id uuid,
  p_task_type text,
  p_visibility text,
  p_status text,
  p_q text
)
RETURNS TABLE(
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
  status text,
  created_at timestamptz,
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
             WHEN 'failed' THEN 'stopped'
             WHEN 'deleted' THEN 'stopped'
             ELSE 'not_deployed'
           END AS status,
           m.created_at
    FROM model_versions m
    JOIN projects p ON p.id = m.project_id AND p.deleted_at IS NULL
    JOIN tenants t ON t.id = m.tenant_id
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
  -- Newest by registration time, not mlflow_version (text: '10' sorts before '2'). Windows run
  -- before DISTINCT ON, so version_count counts every version.
  latest AS (
    SELECT DISTINCT ON (m.project_id, m.model_name)
           m.*,
           count(*) OVER (PARTITION BY m.project_id, m.model_name) AS version_count
    FROM matching m
    ORDER BY m.project_id, m.model_name, m.created_at DESC, m.id DESC
  )
  -- The status filter runs after the fold, on the status the row reports.
  SELECT l.*, count(*) OVER ()
  FROM latest l
  WHERE (p_status IS NULL OR l.status = p_status)
  ORDER BY l.created_at DESC, l.id DESC
  LIMIT p_limit OFFSET p_offset;
$$;

-- Every version of the model one listing row stands for, identified by any one of its versions.
CREATE FUNCTION admin_model_versions(
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

-- Models page stat strip, counted per model with the listing's status vocabulary. Artifact size
-- and review state are not recorded anywhere, so they are not invented here.
CREATE FUNCTION admin_model_stats()
RETURNS TABLE(
  model_count bigint,
  deployed_count bigint,
  deploying_count bigint,
  stopped_count bigint,
  not_deployed_count bigint,
  project_count bigint,
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
         (SELECT count(DISTINCT project_id) FROM latest),
         (SELECT count(*) FROM versions);
$$;

DO $$
DECLARE f regprocedure;
BEGIN
  FOR f IN SELECT p.oid FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
           WHERE n.nspname = 'public' AND p.prosecdef
  LOOP
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', f);
    EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO core_app', f);
  END LOOP;
END
$$;

-- Seed -----------------------------------------------------------------------------------------

-- Demo tenants for exercising isolation, each with the Default team every tenant has.
INSERT INTO tenants (id, zitadel_org_id, name) VALUES
  ('11111111-1111-1111-1111-111111111111', 'demo-org-a', 'Demo Tenant A'),
  ('22222222-2222-2222-2222-222222222222', 'demo-org-b', 'Demo Tenant B');
INSERT INTO teams (tenant_id, name) SELECT id, 'Default' FROM tenants;

-- migrate:down
-- Drops the whole schema. core_app, its CONNECT and its USAGE on public stay: the cluster owns
-- the role and the runtime's logins depend on them.
DROP FUNCTION admin_model_stats(), admin_model_versions(uuid, int, int),
  admin_models(int, int, uuid, text, text, text, text), admin_activity_stats(),
  admin_activity(int, int, uuid, text, text, timestamptz), admin_tenant_detail_stats(uuid),
  admin_tenant_datasets(uuid), admin_tenant_members(uuid), admin_tenant_projects(uuid),
  admin_platform_stats(), admin_tenant_stats(uuid[]), list_public_project_models(uuid),
  list_public_project_datasets(uuid), list_public_projects(int, int),
  inflight_preprocessing_tenant_ids(), inflight_deploy_tenant_ids(), inflight_import_tenant_ids(),
  inflight_training_tenant_ids(), deployment_for_predict(uuid, text), project_exists_any_tenant(uuid);
DROP TABLE rate_limits, signups, dataset_preprocessing_jobs, deployment_test_calls, dataset_imports,
  labeling_projects, model_versions, deployments, training_runs, datasets, usage_events, audit_log,
  project_invitations, project_members, projects, teams, tenants;
