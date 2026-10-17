-- migrate:up
-- Profile data (name, email, avatar) now lives only in Zitadel and reaches the frontend as
-- token claims; core-api keeps no per-user table. Nothing references users(zitadel_user_id):
-- project_members.user_id and audit_log.actor are plain Zitadel ids. Destructive: existing
-- usernames, display names and avatar keys are discarded.
-- migration-lint: allow-destructive (profile data moved to Zitadel)
DROP TABLE IF EXISTS users;

-- migrate:down
-- The dropped profile rows are gone; this only brings back 0023's empty table so that 0023's own
-- down, and re-applying the chain, still work.
CREATE TABLE IF NOT EXISTS users (
  zitadel_user_id text PRIMARY KEY,
  username        text UNIQUE,
  display_name    text,
  avatar_key      text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT, UPDATE ON users TO core_app;
