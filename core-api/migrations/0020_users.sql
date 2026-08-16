-- Identity: profile settings (improvement/phase2/07-identity-oauth-linking.md).
-- Additive per 11-migration-phasing.md step 4. No backfill needed — a user
-- without a row just hasn't visited /settings/profile yet (FR5).

CREATE TABLE IF NOT EXISTS users (
  zitadel_user_id text PRIMARY KEY,
  username        text UNIQUE,
  display_name    text,
  avatar_key      text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

-- No tenant_id / RLS here deliberately: a user can belong to multiple
-- orgs (per Zitadel), so this table is keyed purely on the global Zitadel
-- subject, not tenant-scoped like everything else in this schema.
GRANT SELECT, INSERT, UPDATE ON users TO core_app;
