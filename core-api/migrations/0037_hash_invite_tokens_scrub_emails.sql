-- migrate:up
-- Keep auth secrets and personal data out of the schema beyond the Zitadel user id.
-- Idempotent. The UPDATEs touch RLS-forced project_invitations without an app.tenant_id, which
-- works because backend_owner has BYPASSRLS.

-- project_invitations: store only sha256(token); the plaintext is returned once at creation.
ALTER TABLE project_invitations ADD COLUMN IF NOT EXISTS token_hash text;
DO $$
BEGIN
  IF EXISTS (
    SELECT FROM information_schema.columns
    WHERE table_name = 'project_invitations' AND column_name = 'token'
  ) THEN
    UPDATE project_invitations
       SET token_hash = encode(sha256(convert_to(token, 'UTF8')), 'hex')
     WHERE token_hash IS NULL;
    -- migration-lint: allow-destructive (token_hash above replaces it)
    ALTER TABLE project_invitations DROP COLUMN token;  -- takes its UNIQUE constraint with it
  END IF;
END
$$;
ALTER TABLE project_invitations ALTER COLUMN token_hash SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS project_invitations_token_hash_key ON project_invitations(token_hash);

-- The invited address is only needed until the invite is accepted.
ALTER TABLE project_invitations ALTER COLUMN email DROP NOT NULL;
UPDATE project_invitations SET email = NULL WHERE accepted_at IS NOT NULL;

-- signups: Idempotency-Key reuse is checked against sha256(lower(email)); the plaintext is
-- cleared once the signup is done.
ALTER TABLE signups ADD COLUMN IF NOT EXISTS email_hash text;
UPDATE signups SET email_hash = encode(sha256(convert_to(lower(email), 'UTF8')), 'hex')
 WHERE email_hash IS NULL AND email IS NOT NULL;
ALTER TABLE signups ALTER COLUMN email_hash SET NOT NULL;
ALTER TABLE signups ALTER COLUMN email DROP NOT NULL;
UPDATE signups SET email = NULL WHERE state = 'done';

-- migrate:down
-- Intentionally empty: plaintext invite tokens and scrubbed emails cannot be recovered from their
-- hashes, so there is nothing true to restore. The up is idempotent, so re-applying it is safe, and
-- rolling back further (0035, 0002) drops these tables anyway.
