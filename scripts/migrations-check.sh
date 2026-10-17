#!/bin/sh
# Apply core-api/migrations to an empty PostgreSQL 18 the way the cluster's backend-migrate Job does
# (dbmate, same flags, logged in as a role that switches to backend_owner: no superuser), roll the
# whole chain back, apply it again, and check the grants the app depends on. Needs only Docker.
#
#   ./scripts/migrations-check.sh
set -eu
cd "$(dirname "$0")/.."

DBMATE_IMAGE=ghcr.io/amacneil/dbmate:2.36.0
PG_IMAGE=postgres:18-alpine
name=migrations-check-$$
work=$(mktemp -d)
trap 'docker rm -f "$name" >/dev/null 2>&1 || true; docker network rm "$name" >/dev/null 2>&1 || true; rm -rf "$work"' EXIT

docker network create "$name" >/dev/null
docker run -d --name "$name" --network "$name" -e POSTGRES_PASSWORD=postgres "$PG_IMAGE" >/dev/null
# TCP only answers once the entrypoint's init phase is over and the real server is up.
until docker exec "$name" pg_isready -q -h 127.0.0.1 -U postgres; do sleep 1; done

psql() { docker exec -i "$name" psql -q -U postgres -v ON_ERROR_STOP=1 "$@"; }
psql -v db=backend -v migrator_password=check < scripts/db-roles.sql

echo "DATABASE_URL=postgres://backend_migrator:check@$name:5432/backend?sslmode=disable" > "$work/dbmate.env"
dbmate() {
  docker run --rm --network "$name" \
    -v "$PWD/core-api/migrations:/migrations:ro" -v "$work/dbmate.env:/vault/secrets/dbmate.env:ro" \
    "$DBMATE_IMAGE" --env-file /vault/secrets/dbmate.env --migrations-dir /migrations \
    --migrations-table schema_migrations --wait --no-dump-schema "$@"
}

# Fails (and names the objects) when the schema breaks a rule the cluster relies on.
check() {
  psql -d backend <<'SQL'
DO $$
DECLARE bad text;
BEGIN
  SELECT string_agg(x, '; ') INTO bad FROM (
    SELECT 'not owned by backend_owner: ' || c.relname AS x
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND pg_get_userbyid(c.relowner) <> 'backend_owner'
    UNION ALL
    SELECT 'function not owned by backend_owner: ' || p.oid::regprocedure
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND pg_get_userbyid(p.proowner) <> 'backend_owner'
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
    SELECT 'tenant table without forced RLS: ' || c.relname
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT (c.relrowsecurity AND c.relforcerowsecurity)
       AND EXISTS (SELECT FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'tenant_id')
       AND c.relname NOT IN ('signups')  -- written before its tenant exists (0035)
    UNION ALL
    SELECT 'core_app can read schema_migrations'
     WHERE has_table_privilege('core_app', 'public.schema_migrations', 'SELECT')
    UNION ALL
    SELECT 'core_app can CREATE in public' WHERE has_schema_privilege('core_app', 'public', 'CREATE')
  ) s;
  IF bad IS NOT NULL THEN RAISE EXCEPTION '%', bad; END IF;
END
$$;
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
left=$(psql -d backend -tA -c "SELECT count(*) FROM schema_migrations")
objects=$(psql -d backend -tA -c "SELECT string_agg(c.relname, ', ') FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'public' AND c.relname NOT LIKE 'schema_migrations%'")
funcs=$(psql -d backend -tA -c "SELECT string_agg(p.proname, ', ') FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public'")
[ "$left" = 0 ] && [ -z "$objects" ] && [ -z "$funcs" ] || {
  echo "rollback left: $left versions; relations: $objects; functions: $funcs" >&2; exit 1; }

echo "==> up again"
dbmate up
check
echo "migrations check passed"
