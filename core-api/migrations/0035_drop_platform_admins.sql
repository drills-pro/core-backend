-- platform_admins (zitadel_user_id, note, created_at) once listed who could
-- reach /v1/admin. No migration here creates it any more and nothing reads
-- it: an operator is whoever holds the platform_admin project role in
-- Zitadel (app.core.auth.get_platform_admin), or has a row in
-- admin_accounts (0032) for the password sign-in. Databases set up before
-- that change still carry the empty table; this clears it from them.

DROP TABLE IF EXISTS platform_admins;
