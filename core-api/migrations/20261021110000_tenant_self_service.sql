-- migrate:up
-- Tenant self-service (planning/03, 04): rename, 30-day soft delete, and consented ownership offers.

-- A tenant its owner deleted: blocked like a suspended one, restorable until the purge (not built yet).
ALTER TABLE tenants DROP CONSTRAINT tenants_status_check;
ALTER TABLE tenants
  ADD COLUMN deleted_at timestamptz,
  ADD CONSTRAINT tenants_status_check CHECK (status IN ('active', 'suspended', 'pending_delete'));

-- Rename, delete and restore run as core_app (the admin status route did too, but had no grant).
GRANT UPDATE (name, status, deleted_at) ON tenants TO core_app;

-- /v1/me shows a tenant pending deletion so the console can offer "restore".
DROP FUNCTION my_tenants(text);
CREATE FUNCTION my_tenants(p_sub text) RETURNS TABLE(id uuid, name text, role text, status text)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT t.id, t.name, m.role, t.status FROM tenant_members m JOIN tenants t ON t.id = m.tenant_id
  WHERE m.user_sub = p_sub ORDER BY t.created_at, t.id;
$$;
REVOKE ALL ON FUNCTION my_tenants(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION my_tenants(text) TO core_app;

-- An owner offers ownership to a member, who must accept. At most one open offer per tenant; accepting
-- or cancelling deletes the row.
CREATE TABLE ownership_offers (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL UNIQUE REFERENCES tenants(id),
  from_sub    text NOT NULL,
  to_sub      text NOT NULL,
  step_down   boolean NOT NULL DEFAULT false,
  created_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL
);
ALTER TABLE ownership_offers ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON ownership_offers
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT, DELETE ON ownership_offers TO core_app;

-- migrate:down
DROP TABLE ownership_offers;
DROP FUNCTION my_tenants(text);
CREATE FUNCTION my_tenants(p_sub text) RETURNS TABLE(id uuid, name text, role text)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT t.id, t.name, m.role FROM tenant_members m JOIN tenants t ON t.id = m.tenant_id
  WHERE m.user_sub = p_sub ORDER BY t.created_at, t.id;
$$;
REVOKE ALL ON FUNCTION my_tenants(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION my_tenants(text) TO core_app;
REVOKE UPDATE (name, status, deleted_at) ON tenants FROM core_app;
ALTER TABLE tenants DROP CONSTRAINT tenants_status_check;
-- migration-lint: allow-destructive (down only: pending_delete rows would violate the old CHECK)
ALTER TABLE tenants DROP COLUMN deleted_at;
ALTER TABLE tenants ADD CONSTRAINT tenants_status_check CHECK (status IN ('active', 'suspended'));
