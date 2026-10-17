-- migrate:up
-- Multi-tenancy baseline (req/01-multi-tenancy.md).
-- Applied by dbmate as backend_owner, not a superuser (docs/runtime-contract.md).

-- core_app is a NOLOGIN group role: it holds the actual table grants, and
-- Vault's database secrets engine creates short-lived LOGIN users
-- "IN ROLE core_app" (see scripts/vault-setup.sh) that inherit them.
-- Nobody connects as core_app directly.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'core_app') THEN
    CREATE ROLE core_app NOLOGIN;
  END IF;
END
$$;
ALTER ROLE core_app NOLOGIN;

DO $$
BEGIN
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO core_app', current_database());
END
$$;
-- USAGE only: runtime users never create objects (they would own them, and Vault could not
-- drop them at lease end). Schema changes come from migrations, as backend_owner.
GRANT USAGE ON SCHEMA public TO core_app;

CREATE TABLE IF NOT EXISTS tenants (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  -- Zitadel organization ID (req/02-auth-identity.md FR2: org maps 1:1 to tenant).
  zitadel_org_id text NOT NULL UNIQUE,
  name        text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS projects (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id),
  name        text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS projects_tenant_id_idx ON projects(tenant_id);

ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE projects FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON projects;
CREATE POLICY tenant_isolation ON projects
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON projects TO core_app;
GRANT SELECT ON tenants TO core_app;

-- Demo tenants for exercising isolation before req/02's Zitadel
-- org-provisioning webhook exists to create these rows for real.
INSERT INTO tenants (id, zitadel_org_id, name) VALUES
  ('11111111-1111-1111-1111-111111111111', 'demo-org-a', 'Demo Tenant A'),
  ('22222222-2222-2222-2222-222222222222', 'demo-org-b', 'Demo Tenant B')
ON CONFLICT (id) DO NOTHING;

-- Existence check that bypasses RLS (SECURITY DEFINER runs it as its owner,
-- backend_owner, which has BYPASSRLS) so the API can tell "doesn't exist" (404) apart from
-- "exists in another tenant" (403) per req/01 FR1, without ever returning
-- another tenant's row contents to core_app.
CREATE OR REPLACE FUNCTION project_exists_any_tenant(p_id uuid) RETURNS boolean
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT EXISTS(SELECT 1 FROM projects WHERE id = p_id);
$$;
REVOKE ALL ON FUNCTION project_exists_any_tenant(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION project_exists_any_tenant(uuid) TO core_app;

-- migrate:down
DROP FUNCTION IF EXISTS project_exists_any_tenant(uuid);
DROP TABLE IF EXISTS projects;
DROP TABLE IF EXISTS tenants;
-- core_app, its CONNECT and its USAGE on public stay: the role is shared with the cluster (Vault's
-- core-app users are IN ROLE core_app), and revoking them would cut off every runtime login.
