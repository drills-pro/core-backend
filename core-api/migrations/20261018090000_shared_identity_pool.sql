-- migrate:up
-- ADR-021: one shared Zitadel org holds every human; tenants, membership and roles live here.

-- Claim copy, keyed (issuer, sub) so identities from different realms never collide. Identity is
-- not tenant data, so no RLS (same as rate_limits).
CREATE TABLE principals (
  issuer         text NOT NULL,
  sub            text NOT NULL,
  email          text,
  display_name   text,
  last_seen_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (issuer, sub)
);

CREATE TABLE tenant_members (
  tenant_id  uuid NOT NULL REFERENCES tenants(id),
  user_sub   text NOT NULL,
  role       text NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, user_sub)
);
CREATE INDEX tenant_members_user_idx ON tenant_members(user_sub);

ALTER TABLE tenant_members ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenant_members
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- Tenants are no longer 1:1 with a Zitadel org (the column stays for the admin back-office and
-- legacy rows).
ALTER TABLE tenants ALTER COLUMN zitadel_org_id DROP NOT NULL;

-- Resolving who someone is happens before any tenant context exists, so these two read across
-- tenants: they answer only for the one user_sub asked about.
CREATE FUNCTION my_tenants(p_sub text) RETURNS TABLE(id uuid, name text, role text)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT t.id, t.name, m.role FROM tenant_members m JOIN tenants t ON t.id = m.tenant_id
  WHERE m.user_sub = p_sub ORDER BY t.created_at, t.id;
$$;

CREATE FUNCTION member_role(p_sub text, p_tenant uuid) RETURNS text
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT role FROM tenant_members WHERE user_sub = p_sub AND tenant_id = p_tenant;
$$;

REVOKE ALL ON FUNCTION my_tenants(text), member_role(text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION my_tenants(text), member_role(text, uuid) TO core_app;

GRANT SELECT, INSERT, UPDATE ON principals TO core_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_members TO core_app;

-- The signup saga is replaced by one transaction (POST /v1/tenants). No real signups exist.
-- migration-lint: allow-destructive (saga removed per ADR-021, the table only held in-flight signups)
DROP TABLE signups;

-- migrate:down
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
GRANT SELECT, INSERT, UPDATE, DELETE ON signups TO core_app;
DROP FUNCTION my_tenants(text), member_role(text, uuid);
DROP TABLE tenant_members, principals;
-- migration-lint: allow-destructive (restores the NOT NULL of the init migration; fails if NULL org ids exist)
ALTER TABLE tenants ALTER COLUMN zitadel_org_id SET NOT NULL;
