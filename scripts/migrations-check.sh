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
INSERT INTO tenant_members (tenant_id, user_sub, role) VALUES ('22222222-2222-2222-2222-222222222222', 'u', 'member');
SET LOCAL ROLE core_app;
SET LOCAL app.tenant_id = '11111111-1111-1111-1111-111111111111';
DO $$
BEGIN
  IF (SELECT count(*) FROM projects) <> 1 THEN RAISE EXCEPTION 'core_app sees another tenant''s projects'; END IF;
  IF NOT EXISTS (SELECT FROM accessible_project_ids('u', '22222222-2222-2222-2222-222222222222')) THEN
    RAISE EXCEPTION 'SECURITY DEFINER cannot see across tenants';
  END IF;
END
$$;
ROLLBACK;

-- A foreign tenant sees no dataset versions or imports.
BEGIN;
INSERT INTO projects (id, tenant_id, name) VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '11111111-1111-1111-1111-111111111111', 'a');
INSERT INTO datasets (id, tenant_id, project_id, name)
  VALUES ('dddddddd-dddd-dddd-dddd-dddddddddddd', '11111111-1111-1111-1111-111111111111', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'd');
INSERT INTO dataset_imports (id, tenant_id, project_id, dataset_id, source, idempotency_key)
  VALUES ('eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee', '11111111-1111-1111-1111-111111111111',
          'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'dddddddd-dddd-dddd-dddd-dddddddddddd', 'upload', 'k');
INSERT INTO dataset_versions (tenant_id, dataset_id, number, lakefs_commit, import_id)
  VALUES ('11111111-1111-1111-1111-111111111111', 'dddddddd-dddd-dddd-dddd-dddddddddddd', 1, 'c',
          'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee');
SET LOCAL ROLE core_app;
SET LOCAL app.tenant_id = '22222222-2222-2222-2222-222222222222';
DO $$
BEGIN
  IF EXISTS (SELECT FROM dataset_versions) OR EXISTS (SELECT FROM dataset_imports) THEN
    RAISE EXCEPTION 'core_app sees another tenant''s dataset versions or imports';
  END IF;
END
$$;
ROLLBACK;

-- The retention sweep: purge_dataset_rows removes a dataset and everything pointing at it (as core_app,
-- across tenants), and returns false for a dataset that was restored or is still inside the window.
BEGIN;
INSERT INTO projects (id, tenant_id, name) VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '11111111-1111-1111-1111-111111111111', 'a');
INSERT INTO datasets (id, tenant_id, project_id, name, deleted_at) VALUES
  ('dddddddd-dddd-dddd-dddd-dddddddddddd', '11111111-1111-1111-1111-111111111111', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'gone', now() - interval '2 days'),
  ('ffffffff-ffff-ffff-ffff-ffffffffffff', '11111111-1111-1111-1111-111111111111', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'restored', NULL);
INSERT INTO dataset_imports (id, tenant_id, project_id, dataset_id, source, status, idempotency_key, expires_at)
  VALUES ('eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee', '11111111-1111-1111-1111-111111111111',
          'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'dddddddd-dddd-dddd-dddd-dddddddddddd', 'upload', 'awaiting_upload', 'k', now() - interval '1 day');
INSERT INTO dataset_versions (id, tenant_id, dataset_id, number, lakefs_commit, import_id)
  VALUES ('99999999-9999-9999-9999-999999999999', '11111111-1111-1111-1111-111111111111',
          'dddddddd-dddd-dddd-dddd-dddddddddddd', 1, 'c', 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee');
UPDATE datasets SET published_version_id = '99999999-9999-9999-9999-999999999999' WHERE id = 'dddddddd-dddd-dddd-dddd-dddddddddddd';
UPDATE dataset_imports SET base_version_id = '99999999-9999-9999-9999-999999999999';
UPDATE projects SET default_test_dataset_id = 'dddddddd-dddd-dddd-dddd-dddddddddddd';
INSERT INTO training_runs (tenant_id, project_id, idempotency_key, ray_job_id, dataset_ref, dataset_id, test_dataset_id)
  VALUES ('11111111-1111-1111-1111-111111111111', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'k', 'r', 'x',
          'dddddddd-dddd-dddd-dddd-dddddddddddd', 'dddddddd-dddd-dddd-dddd-dddddddddddd');
SET LOCAL ROLE core_app;
SET LOCAL app.tenant_id = '11111111-1111-1111-1111-111111111111';
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM expired_import_tenant_ids(now()) t WHERE t = '11111111-1111-1111-1111-111111111111') THEN
    RAISE EXCEPTION 'expired_import_tenant_ids missed an expired import';
  END IF;
  IF (SELECT count(*) FROM purgeable_datasets(now() - interval '1 day')) <> 1 THEN
    RAISE EXCEPTION 'purgeable_datasets must list only the deleted dataset past the cutoff';
  END IF;
  IF purge_dataset_rows('dddddddd-dddd-dddd-dddd-dddddddddddd', now() - interval '3 days') THEN
    RAISE EXCEPTION 'purge_dataset_rows purged a dataset still inside the window';
  END IF;
  IF purge_dataset_rows('ffffffff-ffff-ffff-ffff-ffffffffffff', now()) THEN
    RAISE EXCEPTION 'purge_dataset_rows purged a restored dataset';
  END IF;
  IF NOT purge_dataset_rows('dddddddd-dddd-dddd-dddd-dddddddddddd', now() - interval '1 day') THEN
    RAISE EXCEPTION 'purge_dataset_rows did not purge an expired dataset';
  END IF;
  IF purge_dataset_rows('dddddddd-dddd-dddd-dddd-dddddddddddd', now()) THEN
    RAISE EXCEPTION 'purge_dataset_rows must be idempotent once the rows are gone';
  END IF;
END
$$;
RESET ROLE;
DO $$
BEGIN
  IF EXISTS (SELECT FROM datasets WHERE id = 'dddddddd-dddd-dddd-dddd-dddddddddddd')
     OR EXISTS (SELECT FROM dataset_imports) OR EXISTS (SELECT FROM dataset_versions) THEN
    RAISE EXCEPTION 'purge_dataset_rows left rows behind';
  END IF;
  IF NOT EXISTS (SELECT FROM datasets WHERE id = 'ffffffff-ffff-ffff-ffff-ffffffffffff') THEN
    RAISE EXCEPTION 'purge_dataset_rows removed the restored dataset';
  END IF;
  IF EXISTS (SELECT FROM projects WHERE default_test_dataset_id IS NOT NULL)
     OR EXISTS (SELECT FROM training_runs WHERE dataset_id IS NOT NULL OR test_dataset_id IS NOT NULL) THEN
    RAISE EXCEPTION 'purge_dataset_rows left dangling references';
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
