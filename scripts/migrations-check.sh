#!/bin/sh
# Apply core-api/migrations to an empty PostgreSQL 18 the way the cluster's backend-migrate Job does
# (dbmate, same flags, as backend_admin: the database owner, not a superuser), roll everything back,
# apply it again, and check the grants the app depends on. Needs only Docker.
#
#   ./scripts/migrations-check.sh
set -eu
cd "$(dirname "$0")/.."

# The compose postgres (fresh volume, so compose/postgres/core-api.sql runs) and dbmate services, in a
# throwaway project with no published ports, so it runs next to a live dev stack.
work=$(mktemp -d)
export COMPOSE_PROJECT_NAME=migrations-check-$$
printf 'services:\n  postgres:\n    ports: !reset []\n' > "$work/no-ports.yml"
dc() { docker compose -f docker-compose.yml -f "$work/no-ports.yml" "$@"; }
trap 'dc down -v >/dev/null 2>&1 || true; rm -rf "$work"' EXIT

dc up -d --wait postgres
psql() { dc exec -T postgres psql -q -U postgres -v ON_ERROR_STOP=1 "$@"; }
dbmate() { dc run --rm -q dbmate "$@"; }

# Fails (and names the objects) when the schema breaks a rule the cluster relies on.
check() {
  psql -d core <<'SQL'
DO $$
DECLARE bad text;
BEGIN
  SELECT string_agg(x, '; ') INTO bad FROM (
    SELECT 'not owned by backend_admin: ' || c.relname AS x
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND pg_get_userbyid(c.relowner) <> 'backend_admin'
    UNION ALL
    SELECT 'function not owned by backend_admin: ' || p.oid::regprocedure
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND pg_get_userbyid(p.proowner) <> 'backend_admin'
    UNION ALL
    SELECT 'core_app cannot SELECT table ' || c.relname
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND c.relname <> 'schema_migrations'
       AND NOT has_table_privilege('core_app', c.oid, 'SELECT')
    UNION ALL
    SELECT 'core_app cannot use sequence ' || c.relname
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public'
       -- CASE: the planner may evaluate this before the filters, and it errors on non-sequences.
       AND CASE WHEN c.relkind = 'S' THEN NOT has_sequence_privilege('core_app', c.oid, 'USAGE') END
    UNION ALL
    SELECT 'core_app cannot execute ' || p.oid::regprocedure
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND NOT has_function_privilege('core_app', p.oid, 'EXECUTE')
    UNION ALL
    SELECT 'SECURITY DEFINER without search_path: ' || p.oid::regprocedure
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND p.prosecdef
       AND NOT EXISTS (SELECT FROM unnest(p.proconfig) c WHERE c LIKE 'search_path=%')
    UNION ALL
    SELECT 'SECURITY DEFINER executable by PUBLIC: ' || p.oid::regprocedure
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND p.prosecdef
       AND EXISTS (SELECT FROM aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a WHERE a.grantee = 0)
    UNION ALL
    SELECT 'tenant table without RLS: ' || c.relname
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT c.relrowsecurity
       AND EXISTS (SELECT FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'tenant_id')
       AND c.relname NOT IN ('signups')  -- written before its tenant exists
    UNION ALL
    SELECT 'core_app can read schema_migrations'
     WHERE has_table_privilege('core_app', 'public.schema_migrations', 'SELECT')
    UNION ALL
    SELECT 'core_app can CREATE in public' WHERE has_schema_privilege('core_app', 'public', 'CREATE')
  ) s;
  IF bad IS NOT NULL THEN RAISE EXCEPTION '%', bad; END IF;
END
$$;

-- RLS holds for core_app, while SECURITY DEFINER (running as the owner) still sees every tenant.
BEGIN;
INSERT INTO projects (id, tenant_id, name) VALUES
  ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '11111111-1111-1111-1111-111111111111', 'a'),
  ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', '22222222-2222-2222-2222-222222222222', 'b');
SET LOCAL ROLE core_app;
SET LOCAL app.tenant_id = '11111111-1111-1111-1111-111111111111';
DO $$
BEGIN
  IF (SELECT count(*) FROM projects) <> 1 THEN RAISE EXCEPTION 'core_app sees another tenant''s projects'; END IF;
  IF NOT project_exists_any_tenant('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb') THEN
    RAISE EXCEPTION 'SECURITY DEFINER cannot see across tenants';
  END IF;
END
$$;
ROLLBACK;
SQL
}

total=$(ls core-api/migrations/*.sql | wc -l | tr -d ' ')

echo "==> up from empty ($total migrations)"
dbmate up
dbmate up  # a second run must be a no-op
check

echo "==> rollback all $total"
i=0
while [ "$i" -lt "$total" ]; do dbmate rollback; i=$((i + 1)); done
left=$(psql -d core -tA -c "SELECT count(*) FROM schema_migrations")
objects=$(psql -d core -tA -c "SELECT string_agg(c.relname, ', ') FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'public' AND c.relname NOT LIKE 'schema_migrations%'")
funcs=$(psql -d core -tA -c "SELECT string_agg(p.proname, ', ') FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public'")
[ "$left" = 0 ] && [ -z "$objects" ] && [ -z "$funcs" ] || {
  echo "rollback left: $left versions; relations: $objects; functions: $funcs" >&2; exit 1; }

echo "==> up again"
dbmate up
check
echo "migrations check passed"
