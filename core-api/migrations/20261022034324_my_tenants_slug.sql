-- migrate:up
-- /v1/me lists each tenant with its slug, so the console can put the handle in its URLs (/w/<slug>).
DROP FUNCTION my_tenants(text);
CREATE FUNCTION my_tenants(p_sub text) RETURNS TABLE(id uuid, name text, slug text, role text, status text)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT t.id, t.name, t.slug, m.role, t.status FROM tenant_members m JOIN tenants t ON t.id = m.tenant_id
  WHERE m.user_sub = p_sub ORDER BY t.created_at, t.id;
$$;
REVOKE ALL ON FUNCTION my_tenants(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION my_tenants(text) TO core_app;

-- migrate:down
DROP FUNCTION my_tenants(text);
CREATE FUNCTION my_tenants(p_sub text) RETURNS TABLE(id uuid, name text, role text, status text)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT t.id, t.name, m.role, t.status FROM tenant_members m JOIN tenants t ON t.id = m.tenant_id
  WHERE m.user_sub = p_sub ORDER BY t.created_at, t.id;
$$;
REVOKE ALL ON FUNCTION my_tenants(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION my_tenants(text) TO core_app;
