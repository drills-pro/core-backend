-- migrate:up
-- Tenant invitations (planning/04): owner/admin invite an email into the tenant. Only sha256(token) is
-- stored. The invitee is not a member yet, so accepting goes through a SECURITY DEFINER function.
CREATE TABLE tenant_invitations (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id),
  email       text NOT NULL,
  role        text NOT NULL CONSTRAINT tenant_invitations_role_check CHECK (role IN ('admin', 'member')),
  token_hash  text NOT NULL UNIQUE,
  invited_by  text NOT NULL,
  expires_at  timestamptz NOT NULL,
  accepted_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX tenant_invitations_tenant_idx ON tenant_invitations(tenant_id);

ALTER TABLE tenant_invitations ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenant_invitations
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT, UPDATE ON tenant_invitations TO core_app;

-- One statement claims the token and adds the member: of two concurrent accepts only one gets a row.
CREATE FUNCTION accept_tenant_invitation(p_token_hash text, p_email text, p_sub text)
RETURNS TABLE(tenant_id uuid, role text)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH inv AS (
    UPDATE tenant_invitations SET accepted_at = now()
    WHERE token_hash = p_token_hash AND accepted_at IS NULL AND expires_at > now()
      AND lower(email) = lower(p_email)
    RETURNING tenant_invitations.tenant_id AS tid, tenant_invitations.role AS r
  ), mem AS (
    INSERT INTO tenant_members (tenant_id, user_sub, role) SELECT tid, p_sub, r FROM inv
    ON CONFLICT DO NOTHING
  )
  SELECT tid, r FROM inv;
$$;
REVOKE ALL ON FUNCTION accept_tenant_invitation(text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION accept_tenant_invitation(text, text, text) TO core_app;

-- Tenant-level audit rows (member and invitation changes) belong to no project.
ALTER TABLE audit_log ALTER COLUMN project_id DROP NOT NULL;

-- POST /v1/tenants replay: the same (sub, key) returns the tenant the first call made. Not tenant data,
-- so no RLS (same as rate_limits and principals).
CREATE TABLE tenant_create_keys (
  user_sub   text NOT NULL,
  key        text NOT NULL,
  new_tenant uuid NOT NULL,  -- not tenant_id: this row is the caller's, read before any tenant context
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_sub, key)
);
GRANT SELECT, INSERT ON tenant_create_keys TO core_app;

-- migrate:down
DROP TABLE tenant_create_keys;
-- migration-lint: allow-destructive (restores NOT NULL; fails if tenant-level audit rows exist)
ALTER TABLE audit_log ALTER COLUMN project_id SET NOT NULL;
DROP FUNCTION accept_tenant_invitation(text, text, text);
DROP TABLE tenant_invitations;
