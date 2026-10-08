-- What the console's profile and settings pages edit
-- (admin-frontend src/app/(console)/profile, src/app/(console)/settings),
-- held on the account itself, and read and written by
-- app/modules/admin/auth/account.py (/v1/admin/account). Only password
-- accounts have a row here; an operator who signs in through Zitadel has
-- no profile in core-api.
--
-- Profile: display_name and email already exist (0032), and username is
-- the read-only login name; the page adds a phone number.
--
-- Settings: one column per preference rather than a jsonb bag like
-- tenants.settings (0027) — the set is small and fixed, and every one has
-- a default the console relies on, so the database can hold the
-- vocabulary instead of the API. Defaults match the console's own
-- (DEFAULTS in settings-form.tsx), so an account that never opened the
-- page reads the same as one that saved it untouched.
--
-- 0032's table-level SELECT, INSERT, UPDATE grant to core_app already
-- covers new columns.

-- Profile -------------------------------------------------------------------

ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS phone text;

-- Appearance ----------------------------------------------------------------

ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS theme text NOT NULL DEFAULT 'system'
  CONSTRAINT admin_accounts_theme_check CHECK (theme IN ('system', 'light', 'dark'));
ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS table_density text NOT NULL DEFAULT 'comfortable'
  CONSTRAINT admin_accounts_table_density_check CHECK (table_density IN ('comfortable', 'compact'));

-- Language & region ---------------------------------------------------------

ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS language text NOT NULL DEFAULT 'en'
  CONSTRAINT admin_accounts_language_check CHECK (language IN ('en', 'th'));
-- An IANA name. Not checked here: a CHECK cannot consult
-- pg_timezone_names, so the API validates it before writing.
ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS time_zone text NOT NULL DEFAULT 'Asia/Bangkok';
ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS date_format text NOT NULL DEFAULT 'dd/mm/yyyy'
  CONSTRAINT admin_accounts_date_format_check
  CHECK (date_format IN ('dd/mm/yyyy', 'mm/dd/yyyy', 'yyyy-mm-dd', 'd mmm yyyy'));

-- Email notifications -------------------------------------------------------

ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS notify_new_organization boolean NOT NULL DEFAULT true;
ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS notify_deployment_failed boolean NOT NULL DEFAULT true;
ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS notify_suspension boolean NOT NULL DEFAULT true;
ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS weekly_digest boolean NOT NULL DEFAULT false;

-- Security ------------------------------------------------------------------

ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS confirm_destructive boolean NOT NULL DEFAULT true;
