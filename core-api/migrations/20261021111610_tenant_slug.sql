-- migrate:up
-- A tenant's URL-safe handle. The API may name one; any other insert (admin back-office, bootstrap
-- script, seed) gets tenant_default_slug from the trigger. Immutable: core_app has no UPDATE on it.
ALTER TABLE tenants ADD COLUMN slug text;

CREATE FUNCTION tenant_default_slug(p_name text, p_id uuid) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $$
  SELECT coalesce(nullif(b, ''), 'tenant') || '-' || substr(p_id::text, 1, 8)
  FROM (SELECT trim(both '-' from left(trim(both '-' from regexp_replace(lower(p_name), '[^a-z0-9]+', '-', 'g')), 30)) AS b) s
$$;

UPDATE tenants SET slug = tenant_default_slug(name, id);
ALTER TABLE tenants ALTER COLUMN slug SET NOT NULL;
ALTER TABLE tenants ADD CONSTRAINT tenants_slug_key UNIQUE (slug);
ALTER TABLE tenants ADD CONSTRAINT tenants_slug_format
  CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND length(slug) BETWEEN 3 AND 40);

CREATE FUNCTION tenants_set_slug() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
  NEW.slug := coalesce(NEW.slug, tenant_default_slug(NEW.name, NEW.id));
  RETURN NEW;
END $$;
CREATE TRIGGER tenants_set_slug BEFORE INSERT ON tenants FOR EACH ROW EXECUTE FUNCTION tenants_set_slug();

-- migrate:down
DROP TRIGGER tenants_set_slug ON tenants;
DROP FUNCTION tenants_set_slug(), tenant_default_slug(text, uuid);
-- migration-lint: allow-destructive (down only: the slugs are discarded)
ALTER TABLE tenants DROP COLUMN slug;
