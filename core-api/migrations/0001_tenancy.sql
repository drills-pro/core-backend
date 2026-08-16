-- Multi-tenancy baseline (req/01-multi-tenancy.md).
-- Run against the `core` database as the postgres superuser.

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

GRANT CONNECT ON DATABASE core TO core_app;
GRANT USAGE, CREATE ON SCHEMA public TO core_app;

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

-- Existence check that bypasses RLS (runs as the owning superuser via
-- SECURITY DEFINER) so the API can tell "doesn't exist" (404) apart from
-- "exists in another tenant" (403) per req/01 FR1, without ever returning
-- another tenant's row contents to core_app.
CREATE OR REPLACE FUNCTION project_exists_any_tenant(p_id uuid) RETURNS boolean
LANGUAGE sql SECURITY DEFINER AS $$
  SELECT EXISTS(SELECT 1 FROM projects WHERE id = p_id);
$$;
REVOKE ALL ON FUNCTION project_exists_any_tenant(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION project_exists_any_tenant(uuid) TO core_app;
