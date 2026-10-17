-- Prod-shaped migration roles for a local or CI Postgres, run as the postgres superuser:
--
--   psql -U postgres -v db=<database> -v migrator_password=<pw> -f scripts/db-roles.sql
--
-- Mirrors k8s docs/operations.md "Migrator role": backend_admin owns the database, backend_owner owns
-- every migrated object, and backend_migrator stands in for Vault's short-lived core-migrator user
-- (LOGIN, IN ROLE backend_owner, SET role at login). Idempotent.
\set ON_ERROR_STOP on
SET client_min_messages = warning;

SELECT 'CREATE ROLE backend_admin NOLOGIN'
  WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'backend_admin') \gexec
SELECT 'CREATE ROLE backend_owner NOLOGIN'
  WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'backend_owner') \gexec
SELECT 'CREATE ROLE backend_migrator LOGIN'
  WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'backend_migrator') \gexec

ALTER ROLE backend_owner NOLOGIN CREATEROLE BYPASSRLS NOSUPERUSER;
GRANT backend_admin TO backend_owner WITH INHERIT TRUE, SET FALSE;
-- On a fresh server core_app does not exist yet: 0001 creates it as backend_owner (ADMIN implied).
SELECT 'GRANT core_app TO backend_owner WITH ADMIN TRUE, INHERIT FALSE, SET FALSE'
  WHERE EXISTS (SELECT FROM pg_roles WHERE rolname = 'core_app') \gexec

SELECT format('ALTER ROLE backend_migrator LOGIN PASSWORD %L', :'migrator_password') \gexec
GRANT backend_owner TO backend_migrator;
ALTER ROLE backend_migrator SET role = 'backend_owner';

SELECT format('CREATE DATABASE %I OWNER backend_admin', :'db')
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db') \gexec
SELECT format('ALTER DATABASE %I OWNER TO backend_admin', :'db') \gexec
