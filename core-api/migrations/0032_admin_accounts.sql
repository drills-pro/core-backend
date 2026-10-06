-- The admin console's own accounts (app/modules/admin/auth/password.py):
-- a username and password kept here, as a way into the console that does
-- not go through Zitadel at all.
--
-- Every account here is a platform operator — there is no role column,
-- because the console has exactly one role and this table only holds
-- people who have it. Accounts are made and changed with the CLI in that
-- module, never from the console itself.
--
-- Not tenant-scoped and no RLS, like users (0020): operators sit above
-- every tenant, not inside one.

CREATE TABLE IF NOT EXISTS admin_accounts (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  username         text NOT NULL,
  email            text,
  display_name     text,
  -- scrypt, self-describing: "scrypt$<n>$<r>$<p>$<salt>$<hash>", so the
  -- cost can rise later without breaking the hashes already stored.
  password_hash    text NOT NULL,
  -- Tokens issued before this no longer open the console: changing a
  -- password signs out every session that used the old one.
  password_changed_at timestamptz NOT NULL DEFAULT now(),
  -- Signs in no more, and every token it holds stops working at once.
  disabled         boolean NOT NULL DEFAULT false,
  -- Wrong passwords in a row; reset by a right one. Enough of them sets
  -- locked_until, and nothing is checked again until it passes.
  failed_attempts  int NOT NULL DEFAULT 0,
  locked_until     timestamptz,
  last_sign_in_at  timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);

-- Either one signs in, typed in any case, so each has to be unique that way.
CREATE UNIQUE INDEX IF NOT EXISTS admin_accounts_username_idx ON admin_accounts (lower(username));
CREATE UNIQUE INDEX IF NOT EXISTS admin_accounts_email_idx ON admin_accounts (lower(email));

GRANT SELECT, INSERT, UPDATE ON admin_accounts TO core_app;
