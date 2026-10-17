-- The app's Postgres roles and database, shaped like the cluster's, for compose and CI. Mounted into
-- the compose postgres as an init script, so a fresh volume gets them on first start; idempotent,
-- so an older volume can take it by hand (README "Migrations"):
--
--   docker compose exec -T postgres psql -U postgres < scripts/db-roles.sql
--
-- Mirrors k8s: backend_admin is pg-backend's CNPG managed role (LOGIN, CREATEROLE, owner of the
-- database; apps/backend/db/cluster.yaml) and core_app the runtime group role created next to it
-- (bootstrap/18-pg-split.sh). The database is `core` here, `backend` in the cluster. The password
-- is a laptop-only value; in the cluster Vault owns it.
\set ON_ERROR_STOP on
SET client_min_messages = warning;

SELECT 'CREATE ROLE backend_admin LOGIN CREATEROLE'
  WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'backend_admin') \gexec
ALTER ROLE backend_admin PASSWORD 'backend_admin';
SELECT 'CREATE ROLE core_app NOLOGIN'
  WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'core_app') \gexec
GRANT core_app TO backend_admin WITH ADMIN OPTION;

SELECT 'CREATE DATABASE core OWNER backend_admin'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'core') \gexec
