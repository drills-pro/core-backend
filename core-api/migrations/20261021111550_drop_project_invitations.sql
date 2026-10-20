-- migrate:up
-- Project invitations are replaced by tenant invitations (tenant_invitations); no route writes this table.
-- migration-lint: allow-destructive (route removed, invites were short-lived 7-day tokens)
DROP TABLE project_invitations;

-- migrate:down
CREATE TABLE project_invitations (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id),
  project_id  uuid NOT NULL REFERENCES projects(id),
  email       text,
  role        text NOT NULL CONSTRAINT project_invitations_role_check
                CHECK (role IN ('viewer', 'editor', 'dataset_editor', 'model_editor', 'deployer', 'project_admin')),
  invited_by  text NOT NULL,
  expires_at  timestamptz NOT NULL,
  accepted_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now(),
  token_hash  text NOT NULL
);
CREATE INDEX project_invitations_tenant_idx ON project_invitations(tenant_id);
CREATE UNIQUE INDEX project_invitations_token_hash_key ON project_invitations(token_hash);
ALTER TABLE project_invitations ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON project_invitations
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON project_invitations TO core_app;
