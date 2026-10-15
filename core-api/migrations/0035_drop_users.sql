-- Profile data (name, email, avatar) now lives only in Zitadel and reaches the frontend as
-- token claims; core-api keeps no per-user table. Nothing references users(zitadel_user_id):
-- project_members.user_id and audit_log.actor are plain Zitadel ids. Destructive: existing
-- usernames, display names and avatar keys are discarded.
DROP TABLE IF EXISTS users;
