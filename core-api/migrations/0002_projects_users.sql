-- migrate:up
-- Project/user management (req/03-project-user-management.md).
-- Run after 0001.

ALTER TABLE projects ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_constraint WHERE conname = 'projects_status_check') THEN
    ALTER TABLE projects ADD CONSTRAINT projects_status_check
      CHECK (status IN ('active', 'archived', 'pending_delete'));
  END IF;
END
$$;

-- FR2: per-project role, independent of org-level role. user_id is the
-- Zitadel subject (sub claim) — no local users table, Zitadel is the
-- source of truth for identity (req/02).
CREATE TABLE IF NOT EXISTS project_members (
  tenant_id   uuid NOT NULL REFERENCES tenants(id),
  project_id  uuid NOT NULL REFERENCES projects(id),
  user_id     text NOT NULL,
  role        text NOT NULL CHECK (role IN ('project_editor', 'project_viewer')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, user_id)
);

-- FR5: invite by email, role pre-assigned, 7-day expiry.
CREATE TABLE IF NOT EXISTS project_invitations (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id),
  project_id  uuid NOT NULL REFERENCES projects(id),
  email       text NOT NULL,
  role        text NOT NULL CHECK (role IN ('project_editor', 'project_viewer')),
  token       text NOT NULL UNIQUE,
  invited_by  text NOT NULL,
  expires_at  timestamptz NOT NULL,
  accepted_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- FR3: project-level audit log. Append-only — no UPDATE/DELETE grant.
CREATE TABLE IF NOT EXISTS audit_log (
  id          bigserial PRIMARY KEY,
  tenant_id   uuid NOT NULL REFERENCES tenants(id),
  project_id  uuid NOT NULL REFERENCES projects(id),
  actor       text NOT NULL,
  action      text NOT NULL,
  entity_type text NOT NULL,
  entity_id   text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS project_members_tenant_idx ON project_members(tenant_id);
CREATE INDEX IF NOT EXISTS project_invitations_tenant_idx ON project_invitations(tenant_id);
CREATE INDEX IF NOT EXISTS audit_log_tenant_project_idx ON audit_log(tenant_id, project_id, created_at DESC);

ALTER TABLE project_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE project_members FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON project_members;
CREATE POLICY tenant_isolation ON project_members
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

ALTER TABLE project_invitations ENABLE ROW LEVEL SECURITY;
ALTER TABLE project_invitations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON project_invitations;
CREATE POLICY tenant_isolation ON project_invitations
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON audit_log;
CREATE POLICY tenant_isolation ON audit_log
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
-- Audit rows are append-only from the app's perspective: grant INSERT/SELECT
-- only, no UPDATE/DELETE, so a compromised app connection can't rewrite history.
GRANT SELECT, INSERT ON audit_log TO core_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON project_members TO core_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON project_invitations TO core_app;
GRANT USAGE ON SEQUENCE audit_log_id_seq TO core_app;

-- migrate:down
DROP TABLE IF EXISTS audit_log;
DROP TABLE IF EXISTS project_invitations;
DROP TABLE IF EXISTS project_members;
ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_status_check;
ALTER TABLE projects DROP COLUMN IF EXISTS deleted_at;
ALTER TABLE projects DROP COLUMN IF EXISTS status;
